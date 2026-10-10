from pathlib import Path
import base64,re,urllib.request,urllib.error,json,concurrent.futures

root=Path(__file__).parent/'platform'
root.mkdir(exist_ok=True)
tag='android-11.0.0_r48'
trees=[
 ('frameworks/av','media/libstagefright/omx/include'),
 ('frameworks/av','media/libstagefright/foundation/include'),
 ('frameworks/av','media/libstagefright/include'),
 ('frameworks/av','media/libmedia/include'),
 ('frameworks/native','headers'),
 ('frameworks/native','headers/media_plugin'),
 ('frameworks/native','headers/media_plugin/include'),
 ('frameworks/native','libs/nativebase/include'),
 ('frameworks/native','libs/nativewindow/include'),
 ('frameworks/native','libs/ui/include'),
 ('frameworks/native','libs/binder/include'),
 ('system/core','libutils/include'),
 ('system/core','libcutils/include'),
 ('system/core','libsystem/include'),
 ('system/core','base/include'),
 ('system/core','libbacktrace/include'),
 ('system/logging','liblog/include'),
 ('system/core','liblog/include'),
 ('hardware/libhardware','include'),
 ('system/core','libsystem/include'),
 ('system/media','audio/include'),
]
seen=set()
def fetch(name,parent=None):
    if name in seen:return
    seen.add(name)
    dest=root/name
    if dest.exists(): data=dest.read_bytes()
    else:
        choices=list(trees)
        preferred={'utils/':'libutils','cutils/':'libcutils','android-base/':'base/',
                   'log/':'liblog','system/':'libsystem','hardware/':'libhardware'}
        for prefix,prefer in preferred.items():
            if name.startswith(prefix):choices.sort(key=lambda item:prefer not in '/'.join(item))
        if parent:choices=[parent]+choices
        data=None
        for repo,path in choices:
            url=f'https://android.googlesource.com/platform/{repo}/+/refs/tags/{tag}/{path}/{name}?format=TEXT'
            for attempt in range(3):
                try:
                    data=base64.b64decode(urllib.request.urlopen(url,timeout=15).read());break
                except urllib.error.HTTPError as exc:
                    if exc.code==404:break
                except urllib.error.URLError:pass
            if data is not None:break
        if data is None:
            print('Unresolved platform include:',name,flush=True);return
        dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(data)
        print('Fetched',name,flush=True)
    # Only follow Android-specific and same-directory quote headers; C/C++
    # standard and Linux headers are supplied by the checked NDK sysroot.
    for delim,inc in re.findall(rb'#\s*include\s*([<"])([^>"\n]+)',data):
        inc=inc.decode()
        if inc.startswith(('utils/','cutils/','media/','system/','hardware/','android-base/','log/','ui/','nativebase/','binder/','backtrace/','android/')):
            fetch(inc)
        elif delim==b'"' and '/' not in inc:
            fetch(str(Path(name).parent/inc).replace('\\','/'))

for n in ['media/stagefright/omx/SoftVideoDecoderOMXComponent.h',
          'media/stagefright/foundation/AMessage.h','media/hardware/OMXPluginBase.h',
          'utils/String8.h','utils/Log.h']:
    fetch(n)
print('Platform header closure:',len(list(root.rglob('*.h'))),flush=True)
