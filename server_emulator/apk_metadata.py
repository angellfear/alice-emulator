"""Read APK metadata without executing app code."""
from pathlib import Path
import zipfile
import xml.etree.ElementTree as ET

ANDROID='{http://schemas.android.com/apk/res/android}'

def read_apk(path):
    with zipfile.ZipFile(path) as archive:
        info=archive.getinfo('AndroidManifest.xml')
        if info.file_size>8*1024**2:raise ValueError('Слишком большой AndroidManifest.xml')
        manifest=archive.read(info)
    icon=None
    if manifest.lstrip().startswith(b'<'):
        root=ET.fromstring(manifest);application=root.find('application')
        package=root.get('package','');version=root.get(ANDROID+'versionName','1.0');code=root.get(ANDROID+'versionCode','1')
        name=application.get(ANDROID+'label',package) if application is not None else package
        description=''
    else:
        from loguru import logger
        logger.disable('androguard')
        from androguard.core.apk import APK
        apk=APK(str(path))
        if not apk.is_valid_APK():raise ValueError('Не удалось прочитать манифест APK')
        package=apk.get_package();version=apk.get_androidversion_name() or '1.0';code=apk.get_androidversion_code() or '1'
        name=apk.get_app_name() or package
        description=apk.get_attribute_value('application','description') or ''
        if description.startswith('@'):
            try:description=apk.get_res_value(description)
            except Exception:description=''
        icon_path=apk.get_app_icon(max_dpi=640)
        if icon_path:
            with zipfile.ZipFile(path) as archive:
                candidates=[icon_path]+[n for n in archive.namelist() if Path(n).stem==Path(icon_path).stem and n.lower().endswith(('.png','.webp','.jpg','.jpeg'))]
                for candidate in candidates:
                    try:
                        entry=archive.getinfo(candidate)
                        if entry.file_size>2*1024**2:continue
                        raw=archive.read(entry)
                        if raw.startswith(b'\x89PNG\r\n\x1a\n') or raw.startswith(b'\xff\xd8\xff') or (raw[:4]==b'RIFF' and raw[8:12]==b'WEBP'):
                            icon=raw;break
                    except KeyError:continue
    if not package:raise ValueError('Package не найден в APK')
    version_code=int(code,16) if str(code).lower().startswith('0x') else int(code)
    return {'name':name,'package':package,'version':version,'version_code':version_code,'description':description,'icon_bytes':icon}
