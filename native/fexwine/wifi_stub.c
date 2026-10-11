/* windows.devices.wifi.dll stand-in for Wine: activation factory of Windows.Devices.WiFi.WiFiAdapter with the
 * IWiFiAdapterStatics methods. Wine has no Wi-Fi WinRT class; Meta's runtime (OculusAppFramework, Air Link "Ari"
 * dongle code) calls WiFiAdapter.GetDeviceSelector() after login, enumerates devices with it and only calls
 * FromIdAsync for devices found. Without the class, C++/WinRT throws and the runtime fail-fasts (c0000409).
 * GetDeviceSelector returns a selector that matches no device; the async methods report E_NOTIMPL (never reached
 * when nothing is found). Unknown interface requests are logged to stderr.
 */
#define COBJMACROS
#include <windows.h>
#include <winstring.h>
#include <stdio.h>

typedef struct IInspectableVtbl_ { void *s[6]; } dummy_t;

static const GUID IID_IUnknown_ = {0x00000000,0x0000,0x0000,{0xc0,0x00,0x00,0x00,0x00,0x00,0x00,0x46}};
static const GUID IID_IInspectable_ = {0xaf86e2e0,0xb12d,0x4c6a,{0x9c,0x5a,0xd7,0xaa,0x65,0x10,0x1e,0x90}};
static const GUID IID_IAgileObject_ = {0x94ea2b94,0xe9cc,0x49e0,{0xc0,0xff,0xee,0x64,0xca,0x8f,0x5b,0x90}};
static const GUID IID_IActivationFactory_ = {0x00000035,0x0000,0x0000,{0xc0,0x00,0x00,0x00,0x00,0x00,0x00,0x46}};
/* Windows.Devices.WiFi.IWiFiAdapterStatics */
static const GUID IID_IWiFiAdapterStatics = {0xda25fddd,0xd24c,0x43e3,{0xaa,0xbd,0xc4,0x65,0x9f,0x73,0x0f,0x99}};

typedef struct statics { const void **vtbl; } statics;

static HRESULT WINAPI f_QueryInterface(statics *This, REFIID iid, void **out)
{
    if (IsEqualGUID(iid, &IID_IUnknown_) || IsEqualGUID(iid, &IID_IInspectable_) || IsEqualGUID(iid, &IID_IAgileObject_)
        || IsEqualGUID(iid, &IID_IActivationFactory_) || IsEqualGUID(iid, &IID_IWiFiAdapterStatics))
    {
        *out = This;
        return S_OK;
    }
    fprintf(stderr, "wifi_stub: unknown interface {%08lx-%04x-%04x-%02x%02x-%02x%02x%02x%02x%02x%02x}\n", iid->Data1,
            iid->Data2, iid->Data3, iid->Data4[0], iid->Data4[1], iid->Data4[2], iid->Data4[3], iid->Data4[4],
            iid->Data4[5], iid->Data4[6], iid->Data4[7]);
    *out = NULL;
    return E_NOINTERFACE;
}
static ULONG WINAPI f_AddRef(statics *This) { return 2; }
static ULONG WINAPI f_Release(statics *This) { return 1; }
static HRESULT WINAPI f_GetIids(statics *This, ULONG *count, IID **iids)
{
    IID *p = CoTaskMemAlloc(sizeof(IID));
    if (!p) return E_OUTOFMEMORY;
    *p = IID_IWiFiAdapterStatics; *count = 1; *iids = p;
    return S_OK;
}
static HRESULT WINAPI f_GetRuntimeClassName(statics *This, HSTRING *name)
{
    static const WCHAR n[] = L"Windows.Devices.WiFi.WiFiAdapter";
    return WindowsCreateString(n, ARRAYSIZE(n) - 1, name);
}
static HRESULT WINAPI f_GetTrustLevel(statics *This, int *level) { *level = 0; return S_OK; }
/* slot 6: IWiFiAdapterStatics::FindAllAdaptersAsync (and IActivationFactory::ActivateInstance: not activatable) */
static HRESULT WINAPI f_FindAllAdaptersAsync(statics *This, void **op) { if (op) *op = NULL; return E_NOTIMPL; }
static HRESULT WINAPI f_GetDeviceSelector(statics *This, HSTRING *value)
{
    /* an interface class no device has: device enumeration with it finds nothing */
    static const WCHAR sel[] = L"System.Devices.InterfaceClassGuid:=\"{f0e2c0a5-7b3e-4f61-9b47-3d1c0f0e2a11}\" AND "
                               L"System.Devices.InterfaceEnabled:=System.StructuredQueryType.Boolean#True";
    fprintf(stderr, "wifi_stub: GetDeviceSelector\n");
    return WindowsCreateString(sel, ARRAYSIZE(sel) - 1, value);
}
static HRESULT WINAPI f_FromIdAsync(statics *This, HSTRING id, void **op) { if (op) *op = NULL; return E_NOTIMPL; }
static HRESULT WINAPI f_RequestAccessAsync(statics *This, void **op) { if (op) *op = NULL; return E_NOTIMPL; }

static const void *vtbl[] = { f_QueryInterface, f_AddRef, f_Release, f_GetIids, f_GetRuntimeClassName,
                              f_GetTrustLevel, f_FindAllAdaptersAsync, f_GetDeviceSelector, f_FromIdAsync,
                              f_RequestAccessAsync };
static statics factory = { vtbl };

HRESULT WINAPI DllGetActivationFactory(HSTRING classid, void **out)
{
    const WCHAR *s = WindowsGetStringRawBuffer(classid, NULL);
    fprintf(stderr, "wifi_stub: DllGetActivationFactory %ls\n", s ? s : L"(null)");
    if (!s || wcscmp(s, L"Windows.Devices.WiFi.WiFiAdapter")) { *out = NULL; return CLASS_E_CLASSNOTAVAILABLE; }
    *out = &factory;
    return S_OK;
}
HRESULT WINAPI DllCanUnloadNow(void) { return S_FALSE; }
