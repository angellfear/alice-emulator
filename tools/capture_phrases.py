"""Захват образцов директив настоящего сервера: озвучка фраз в колонку и разбор журнала.

Для пополнения образцов по «ГЛАВНОЕ ПРАВИЛО» из tasks/PLAN-station-skills.md: чтобы
сценарий отдал те же директивы, что настоящий сервер, нужен образец его ответа на ту же
фразу. Утилита проговаривает фразы синтезом движка TTS эмулятора через колонки ПК —
микрофон колонки слышит их как голос пользователя; колонка будится, настоящий сервер
(маршрут «Прозрачно» + опция «Запись» на устройстве) отвечает, утилита сразу снимает
снапшот журнала и раскладывает его tasks/tools/device_directives.py.

Важно: реплики, синтезированные через /api/tts эмулятора, обрываются на конце —
ogg-стрим не отдаёт хвост фразы. Поэтому синтез идёт напрямую с движка TTS
(тот же, что выбран в настройках эмулятора, вкладка «Голос»), к WAV добавляется
хвост тишины. Нужен ffmpeg (ffplay) в PATH и запущенный движок TTS.

Каждая фраза разбирается отдельно, до перехода к следующей: журнал хранит только
последние 10 записей на пару «устройство — ключ события», при пакетной озвучке
ранние образцы вытесняются. Если колонка не разбудилась, фраза повторяется запасной
формулировкой (второй столбец файла); так и не услышанные — помечаются «ПРОМАХ».

Использование:
    python tools/capture_phrases.py --device <SN> "Алиса, включи музыку" "Алиса, пауза"
    python tools/capture_phrases.py --device <SN> --file phrases.txt
Формат файла: одна фраза на строку; после « | » — запасная формулировка для повтора:
    Алиса, включи музыку | Алиса, включи музыку пожалуйста

Артефакты (--out, по умолчанию временная папка capture_phrases-*):
    phrase_NN_directives.txt — разбор снапшота device_directives.py;
    phrase_NN_body.txt       — полные тела ответов (AliceResponse/Deferred);
    phrase_NN_asr.txt        — распознанный текст (AsrResult);
    phrase_NN.wav            — озвучка; snap_NN.sqlite3 — снапшот (--keep-snap).
Итогный журнал хода — progress.log в той же папке.

Устройство должно быть в маршруте «Прозрачно» с «Записью» (иначе отвечать будет
эмулятор, а не настоящий сервер). Серийник в репозиторий не попадает: он только
аргумент командной строки.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
import wave
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WAKE_PAUSE = re.compile(r"^Алиса,\s*")


def log(out: Path, msg: str):
    line = f"{time.strftime('%H:%M:%S')} | {msg}"
    with (out / "progress.log").open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line, flush=True)


def synth(engine: str, text: str, path: Path, silence: float) -> float:
    """Синтез движком TTS → моно-WAV (int16) с хвостом тишины; длительность, с."""
    req = urllib.request.Request(engine, data=json.dumps({"input": text}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=180) as r:
        body = r.read()
        rate = int(r.headers.get("X-Audio-Sample-Rate", "24000"))
    n = len(body) // 4
    vals = list(struct.unpack(f"<{n}f", body)) if n else []
    peak = max((abs(v) for v in vals), default=1.0) or 1.0
    frames = struct.pack(f"<{len(vals) + int(rate * silence)}h",
                         *[max(-32767, min(32767, int(v / peak * 32000)))
                           for v in vals + [0.0] * int(rate * silence)])
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)
    return len(body) / 4 / rate + silence


def play(path: Path, atempo: float, volume_db: float):
    filters = f"volume={volume_db}dB" + (f",atempo={atempo}" if atempo != 1.0 else "")
    subprocess.run(["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet",
                    "-af", filters, str(path)], check=False, timeout=90)


def wait_session(journal: Path, device: str, since_ts: float, limit: float) -> bool:
    """Ждёт голосовую сессию устройства: VoiceInput и ответ сервера в журнале."""
    deadline = time.time() + limit
    while time.time() < deadline:
        time.sleep(5)
        con = sqlite3.connect(f"file:{journal}?mode=ro", uri=True)
        found = con.execute(
            "SELECT COUNT(*) FROM exchanges WHERE kind='ws' AND device_sn=?"
            " AND key LIKE '%VoiceInput%' AND ts > ?", (device, since_ts)).fetchone()[0]
        answered = con.execute(
            "SELECT COUNT(*) FROM exchanges WHERE kind='ws' AND device_sn=?"
            " AND key LIKE '%AliceResponse%' AND ts > ?", (device, since_ts)).fetchone()[0]
        con.close()
        if found and answered:
            return True
    return False


def snapshot(journal: Path, dst: Path, device: str, since_ts: float):
    """Копия журнала только с кадрами этого устройства с момента старта фразы."""
    src = sqlite3.connect(f"file:{journal}?mode=ro", uri=True)
    out = sqlite3.connect(dst)
    src.backup(out)
    out.execute("DELETE FROM exchanges WHERE device_sn != ? OR ts < ?", (device, since_ts))
    out.execute("DELETE FROM ws_frames")
    out.execute("DELETE FROM journal_groups")
    out.commit()
    out.close()
    src.close()


def body_text(raw) -> str:
    try:
        import zstandard
        return zstandard.ZstdDecompressor().decompress(raw).decode("utf-8", "replace")
    except Exception:
        return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else (raw or "")


def parse(out: Path, tool: Path, snap: Path, idx: int, tag: str) -> dict:
    """Разбор снапшота: директивы (device_directives.py), тела ответов, распознанное."""
    con = sqlite3.connect(snap)
    rows = list(con.execute(
        "SELECT key, request_body, response_body FROM exchanges WHERE kind='ws' ORDER BY ts"))
    con.close()
    asr, answers = [], []
    for key, req, resp in rows:
        for raw in (req, resp):
            text = body_text(raw)
            if "AsrResult" in key:
                asr += [m for m in re.findall(r"[\x20-\x7eа-яёА-ЯЁ?!,\.\- ]{8,120}", text)
                        if re.search(r"[а-яё]{4}", m)]
            if "AliceResponse" in key:
                answers.append((key, text))
    (out / f"phrase_{idx:02d}_asr{tag}.txt").write_text("\n".join(dict.fromkeys(asr)), encoding="utf-8")
    (out / f"phrase_{idx:02d}_body{tag}.txt").write_text(
        "\n\n".join(f"===== {k} =====\n{s}" for k, s in answers), encoding="utf-8")
    result = subprocess.run([sys.executable, str(tool), str(snap)],
                            cwd=ROOT, capture_output=True, text=True, timeout=120)
    (out / f"phrase_{idx:02d}_directives{tag}.txt").write_text(result.stdout, encoding="utf-8")
    reply = []
    for _, text in answers:
        reply += [m for m in re.findall(r"[\x20-\x7eа-яёА-ЯЁ?!,\.\-]{12,200}", text)
                  if re.search(r"[а-яёА-ЯЁ]{4}", m) and "<speaker" not in m]
    return {"directives": sorted(set(re.findall(r"\|\s+([a-z_@]+)", result.stdout))),
            "replies": list(dict.fromkeys(reply))[:3], "responses": len(answers)}


def attempt(out: Path, args, idx: int, phrase: str, tag: str) -> dict | None:
    wav = out / f"phrase_{idx:02d}{tag}.wav"
    text = WAKE_PAUSE.sub("Алиса... ", phrase) if not args.no_wake_pause else phrase
    duration = synth(args.engine, text, wav, args.silence)
    log(out, f"  синтез{tag}: {duration:.1f} с | {text}")
    if args.pre_delay:
        # колонка доигрывает предыдущий ответ / начинает трек — кадрам этой фразы нужен чистый момент
        time.sleep(args.pre_delay)
    since = time.time() - 3
    play(wav, args.atempo, args.volume_db)
    if not wait_session(args.journal, args.device, since, args.wait):
        return None
    snap = out / f"snap_{idx:02d}{tag}.sqlite3"
    snapshot(args.journal, snap, args.device, since)
    parsed = parse(out, args.tool, snap, idx, tag)
    log(out, f"  ЗАХВАЧЕНО{tag}: ответов={parsed['responses']}, "
             f"директивы={', '.join(parsed['directives'][:8])}")
    for reply in parsed["replies"]:
        log(out, f"    ответ: {reply[:120]}")
    if not args.keep_snap:
        snap.unlink()
    return parsed


def main():
    parser = argparse.ArgumentParser(description="Захват образцов директив озвучкой фраз в колонку")
    parser.add_argument("phrases", nargs="*", help="фразы (с ключевым словом колонки)")
    parser.add_argument("--file", help="файл фраз: строка = фраза [| запасная формулировка]")
    parser.add_argument("--device", required=True, help="SN колонки (маршрут «Прозрачно», «Запись» включена)")
    parser.add_argument("--out", type=Path, help="папка артефактов (по умолчанию временная)")
    parser.add_argument("--engine", default="http://127.0.0.1:8778/v1/audio/speech",
                        help="движок TTS из настроек эмулятора (вкладка «Голос»)")
    parser.add_argument("--journal", type=Path, default=ROOT / "server_emulator/state/traffic.sqlite3")
    parser.add_argument("--tool", type=Path, default=ROOT / "tasks/tools/device_directives.py",
                        help="разборщик снапшота")
    parser.add_argument("--wait", type=float, default=26, help="ожидание сессии, с")
    parser.add_argument("--pre-delay", type=float, default=0,
                        help="пауза перед озвучкой каждой фразы, с (дать колонке начать трек/доиграть ответ)")
    parser.add_argument("--silence", type=float, default=0.6, help="хвост тишины в WAV, с")
    parser.add_argument("--atempo", type=float, default=0.9, help="замедление воспроизведения")
    parser.add_argument("--volume-db", type=float, default=12.0, help="усиление воспроизведения, дБ")
    parser.add_argument("--no-wake-pause", action="store_true", help="не менять «Алиса,» на «Алиса... »")
    parser.add_argument("--keep-snap", action="store_true", help="не удалять снапшоты")
    args = parser.parse_args()

    phrases = [(p, None) for p in args.phrases]
    if args.file:
        for line in Path(args.file).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                main_phrase, _, alt = line.partition("|")
                phrases.append((main_phrase.strip(), alt.strip() or None))
    if not phrases:
        parser.error("нет фраз: передайте их аргументами или через --file")

    out = args.out or Path(tempfile.mkdtemp(prefix="capture_phrases-"))
    out.mkdir(parents=True, exist_ok=True)
    log(out, f"ЗАХВАТ: {len(phrases)} фраз(ы), устройство {args.device}")
    for idx, (phrase, alt) in enumerate(phrases):
        log(out, f"PHRASE {idx}: {phrase}")
        try:
            parsed = attempt(out, args, idx, phrase, "")
        except Exception as exc:
            log(out, f"  ошибка основной попытки: {exc}")
            parsed = None
        if not parsed and alt:
            log(out, "  не разбудилась — пробую запасной формулировкой")
            try:
                parsed = attempt(out, args, idx, alt, "_alt")
            except Exception as exc:
                log(out, f"  ошибка запасной попытки: {exc}")
                parsed = None
        if not parsed:
            log(out, f"  ПРОМАХ: фраза не услышана, нужна запись владельцем")
    log(out, "ГОТОВО")


if __name__ == "__main__":
    main()
