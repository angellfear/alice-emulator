"""Заставки главного экрана (gproxy): выбор коллекций фонов — те же ответы, что у
настоящего сервера.

Наблюдение по журналу (.85, «Прозрачно»): POST save_teaser_settings несёт полный
набор включённых коллекций (field 3 = 1 у включённых), ответ настоящего сервера —
200 с пустым телом; GET get_teaser_settings отдаёт тот же каталог, у включённых
коллекций — field 2 = 1 (у .85 маркеры ответа совпали с флагами её save).
Эмулятор хранит выбор в состоянии устройства и зеркалит его в GET; без сохранённого
выбора — каталог без маркеров. Пара для афиши — save_afisha_settings (у настоящего
сервера принимается; без обработчика эмулятора была 404 и цикл «Фоны обновляются»)
и get_afisha_settings (маркеры включённых подкатегорий — тем же соглашением, что у
коллекций). get_ya_disk_settings у настоящего сервера — пустое тело.

Ответы уходят до каталога эндпоинтов (stateful-маршруты не затеняются — как routes
Квазара) и пишутся в журнал «Запись» (local_handler знает эти пути).
"""
from .protowire import encode

CONTENT_TYPE = 'application/x-protobuf'

HANDLES = frozenset({
    ('rpc.alice.yandex.net', 'POST', '/gproxy/get_teaser_settings'),
    ('rpc.alice.yandex.net', 'POST', '/gproxy/save_teaser_settings'),
    ('rpc.alice.yandex.net', 'POST', '/gproxy/get_afisha_settings'),
    ('rpc.alice.yandex.net', 'POST', '/gproxy/save_afisha_settings'),
    ('rpc.alice.yandex.net', 'POST', '/gproxy/get_ya_disk_settings'),
})


def handles(host, method, path):
    return ((host or '').lower(), method, path or '/') in HANDLES


TEASER_CATALOG = [
    [
        (1, [(1, 'YaBroBackgrounds'), (2, 'centaur_choice')]),
        (3, 'Выбор Алисы'),
        (6, 'https://yastatic.net/s3/smart-display-res/backgrounds/settings/centaur_choice_preview.png'),
        (7, 'Обновляемые заставки \nиз лучших коллекций'),
    ],
    [
        (1, [(1, 'YaDiskPhoto')]),
        (3, 'Ваш Яндекс Диск'),
        (6, 'https://yastatic.net/s3/smart-display-res/backgrounds/settings/ya_disk_photo_preview.png'),
        (7, 'Фотографии из альбома'),
    ],
    [
        (1, [(1, 'Afisha')]),
        (3, 'Афиша'),
        (6, 'https://yastatic.net/s3/smart-display-res/backgrounds/settings/afisha_preview.png'),
    ],
    [
        (1, [(1, 'Tretyakovka')]),
        (3, 'Моя Третьяковка'),
        (6, 'https://yastatic.net/s3/smart-display-res/backgrounds/settings/tretyakovka_preview.png'),
        (7, 'Чтобы узнать о картине, на главном экране скажите: «Алиса, расскажи о картине» или коснитесь экрана'),
    ],
    [
        (1, [(1, 'YaBroBackgrounds'), (2, 'live_backgrounds')]),
        (3, 'Живой фон'),
        (6, 'https://yastatic.net/s3/smart-display-res/backgrounds/settings/live_backgrounds_preview.png'),
    ],
    [
        (1, [(1, 'YaBroBackgrounds'), (2, 'deep_backgrounds')]),
        (3, 'Объёмные'),
        (6, 'https://yastatic.net/s3/smart-display-res/backgrounds/settings/deep_backgrounds_preview.png'),
    ],
    [
        (1, [(1, 'YaBroBackgrounds'), (2, 'all_flowers_backgrounds')]),
        (3, 'Все цветы'),
        (6, 'https://yastatic.net/s3/smart-display-res/backgrounds/settings/all_flowers_preview.png'),
    ],
    [
        (1, [(1, 'YaBroBackgrounds'), (2, '224')]),
        (3, 'Изумительные пейзажи'),
        (6, 'https://yastatic.net/s3/bro-bg-store/940be091-93b0-4bc2-b8ba-d2a29c58803c.jpg'),
    ],
    [
        (1, [(1, 'YaBroBackgrounds'), (2, '351')]),
        (3, 'Рассветы'),
        (6, 'https://yastatic.net/s3/bro-bg-store/36f45d8e-6de6-4d81-b9eb-6f0e8adf6c0c.jpg'),
    ],
    [
        (1, [(1, 'YaBroBackgrounds'), (2, '350')]),
        (3, 'Озера'),
        (6, 'https://yastatic.net/s3/bro-bg-store/697ce67e-b787-4fab-8308-38262ebb125e.jpg'),
    ],
    [
        (1, [(1, 'YaBroBackgrounds'), (2, '188')]),
        (3, 'Маяки'),
        (6, 'https://yastatic.net/s3/bro-bg-store/7e0a980e-e625-4185-9d16-7953f7000b60.jpg'),
    ],
    [
        (1, [(1, 'YaBroBackgrounds'), (2, '260')]),
        (3, 'Подводный мир'),
        (6, 'https://yastatic.net/s3/bro-bg-store/e76aae24-e98d-4f9a-a86f-2ace8d82ddac.jpg'),
    ],
    [
        (1, [(1, 'YaBroBackgrounds'), (2, '155')]),
        (3, 'Санкт-Петербург'),
        (6, 'https://yastatic.net/s3/bro-bg-store/83e2693e-0831-4b6d-962f-6282f7bba87d.jpg'),
    ],
    [
        (1, [(1, 'YaBroBackgrounds'), (2, '8')]),
        (3, 'Море'),
        (6, 'https://yastatic.net/s3/bro-bg-store/9fe4e2f9-5398-4c51-83ca-67bff2df5a88.jpg'),
    ],
    [
        (1, [(1, 'YaBroBackgrounds'), (2, '226')]),
        (3, 'Норвегия'),
        (6, 'https://yastatic.net/s3/bro-bg-store/491c1f98-3827-4aae-947d-bb2d289f36c3.jpg'),
    ],
    [
        (1, [(1, 'YaBroBackgrounds'), (2, '9')]),
        (3, 'Природа'),
        (6, 'https://yastatic.net/s3/bro-bg-store/2b29649e-32d6-4faf-b4a4-950714378b3b.jpg'),
    ],
    [
        (1, [(1, 'YaBroBackgrounds'), (2, '347')]),
        (3, 'Небо'),
        (6, 'https://yastatic.net/s3/bro-bg-store/127e1eb0-b0f0-456d-820e-ccaaf160a8a1.jpg'),
    ],
    [
        (1, [(1, 'YaBroBackgrounds'), (2, '12')]),
        (3, 'Транспорт'),
        (6, 'https://yastatic.net/s3/bro-bg-store/677f4b70-9037-4ae4-b7fa-0c0f04cad98b.jpg'),
    ],
    [
        (1, [(1, 'YaBroBackgrounds'), (2, '6')]),
        (3, 'Горы'),
        (6, 'https://yastatic.net/s3/bro-bg-store/4322685b-00fe-439b-821f-08946bd8f693.jpg'),
    ],
    [
        (1, [(1, 'YaBroBackgrounds'), (2, '309')]),
        (3, 'Острова'),
        (6, 'https://yastatic.net/s3/bro-bg-store/46988575-983a-405f-8f7b-495fa1e1c95e.jpg'),
    ],
    [
        (1, [(1, 'YaBroBackgrounds'), (2, '344')]),
        (3, 'Арктика и Антарктика'),
        (6, 'https://yastatic.net/s3/bro-bg-store/cac79acd-7a5f-4a74-b6fb-cc210751fc7e.jpg'),
    ],
    [
        (1, [(1, 'YaBroBackgrounds'), (2, '262')]),
        (3, 'Парки России'),
        (6, 'https://yastatic.net/s3/bro-bg-store/514e647d-53dd-417b-b89f-a4b874c96c18.jpg'),
    ],
    [
        (1, [(1, 'YaBroBackgrounds'), (2, '263')]),
        (3, 'Туманные пейзажи'),
        (6, 'https://yastatic.net/s3/bro-bg-store/7e38fb28-1fa8-4bad-92b6-7a6216c8f458.jpg'),
    ],
    [
        (1, [(1, 'YaBroBackgrounds'), (2, '154')]),
        (3, 'Москва'),
        (6, 'https://yastatic.net/s3/bro-bg-store/3f581ed1-fc6c-4969-b868-7175ea829913.jpg'),
    ],
    [
        (1, [(1, 'YaBroBackgrounds'), (2, '225')]),
        (3, 'Современная архитектура'),
        (6, 'https://yastatic.net/s3/bro-bg-store/019d2667-2a1d-48a4-87c3-a5d495602ad6.jpg'),
    ],
    [
        (1, [(1, 'YaBroBackgrounds'), (2, '117')]),
        (3, 'Вид сверху'),
        (6, 'https://yastatic.net/s3/bro-bg-store/b05e2bd3-d1f8-4c00-b3d4-a11d9250e62e.jpg'),
    ],
    [
        (1, [(1, 'YaBroBackgrounds'), (2, '11')]),
        (3, 'Спорт'),
        (6, 'https://yastatic.net/s3/bro-bg-store/0d1ab1fe-c738-4478-9018-409ce049912a.jpg'),
    ],]

AFISHA_CATALOG = [
    [
        (1, [(1, 'personal-top')]),
        (3, 'По вашим интересам'),
        (4, 'https://yastatic.net/s3/smart-display-res/backgrounds/settings/afisha_preview.png'),
    ],
    [
        (1, [(1, 'personal-concert-top-main')]),
        (3, 'Концерты для вас'),
        (4, 'https://yastatic.net/s3/smart-display-res/backgrounds/settings/afisha_preview.png'),
    ],
]

def _strings(node):
    """Все строковые листья поддерева (вложенность id в запросах разная)."""
    if isinstance(node, str):
        return [node]
    out = []
    if isinstance(node, list):
        for x in node:
            out += _strings(x[1] if isinstance(x, tuple) else x)
    return out


def _wrappers(body):
    """(номер, значение)-разбор тела: список обёрток элементов из field 1 корня."""
    from .protowire import decode
    root = decode(body or b'')
    for n, v in root:
        if n == 1 and isinstance(v, list):
            return v
    return []


def _key(wrapper):
    for n, v in wrapper[1]:
        if n == 1:
            return tuple(_strings(v))
    return ()


def parse_save(body, mark):
    """(разобралось ли, включённые коллекции) из save-запроса: ключи обёрток
    с маркером (mark, 1). Битое тело — (False, пусто): сохранять нечего."""
    try:
        from .protowire import decode
        root = decode(body or b'')
    except Exception:  # noqa: BLE001 — станция прислала не protobuf
        return False, frozenset()
    wrappers = []
    for n, v in root:
        if n == 1 and isinstance(v, list):
            wrappers = v
            break
    return True, frozenset(_key(w) for w in wrappers
                           if any(n == mark and v == 1 for n, v in w[1]))


def compose_get(catalog, enabled):
    """Каталог GET: у включённых коллекций field 2 = 1 после идентификатора
    (наблюдённый порядок байт образца)."""
    items = []
    for children in catalog:
        fields = list(children)
        if _key((1, fields)) in enabled:
            fields.insert(1, (2, 1))
        items.append((1, fields))
    return encode([(1, items)])


def _state(runtime, sn):
    device = runtime.identities.resolve_sn(sn) if sn else None
    return device, ((device or {}).get('state') or {}).get('screensaver') or {}


def _store(runtime, sn, patch):
    device = runtime.identities.resolve_sn(sn) if sn else None
    if device is None:
        return  # устройство не опознано: сохранить некому, ответ как у настоящего сервера
    state = dict(device.get('state') or {})
    merged = dict(state.get('screensaver') or {})
    merged.update(patch)
    state['screensaver'] = merged
    runtime.identities.mutate({'operation': 'state', 'sn': sn, 'state': state})


def answer(runtime, sn, method, path, body):
    """Ответ встроенного обработчика заставок или None (путь не из семейства)."""
    route = ((method or '').upper(), path or '/')
    if route == ('POST', '/gproxy/save_teaser_settings'):
        ok, enabled = parse_save(body, 3)
        if ok:
            _store(runtime, sn, {'teaser': sorted(map(list, enabled))})
        return 200, b'', {'Content-Type': CONTENT_TYPE}
    if route == ('POST', '/gproxy/save_afisha_settings'):
        ok, enabled = parse_save(body, 2)
        if ok:
            _store(runtime, sn, {'afisha': sorted(map(list, enabled))})
        return 200, b'', {'Content-Type': CONTENT_TYPE}
    if route == ('POST', '/gproxy/get_teaser_settings'):
        _device, state = _state(runtime, sn)
        return 200, compose_get(TEASER_CATALOG, {tuple(k) for k in state.get('teaser') or []}),             {'Content-Type': CONTENT_TYPE}
    if route == ('POST', '/gproxy/get_afisha_settings'):
        _device, state = _state(runtime, sn)
        return 200, compose_get(AFISHA_CATALOG, {tuple(k) for k in state.get('afisha') or []}),             {'Content-Type': CONTENT_TYPE}
    if route == ('POST', '/gproxy/get_ya_disk_settings'):
        # настоящий сервер отвечает пустым телом (образец журнала .85)
        return 200, b'', {'Content-Type': CONTENT_TYPE}
    return None
