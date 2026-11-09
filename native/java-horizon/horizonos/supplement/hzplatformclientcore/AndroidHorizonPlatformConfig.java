package horizonos.supplement.hzplatformclientcore;

import android.content.Context;

/**
 * What Meta's Horizon Platform SDK (com.meta.xr.sdk.platform 2xx, Oculus.Platform AndroidClient) passes to
 * HorizonPlatformCore.setup: the game's activity and its Meta app id. On Horizon OS this class comes from the
 * hzplatformclientcore supplement; frame.horizon_platform_stub adds this stand-in to the game's own APK.
 */
public final class AndroidHorizonPlatformConfig {
    public final Context context;
    public final String appId;

    public AndroidHorizonPlatformConfig(Context context, String appId) {
        this.context = context;
        this.appId = appId;
    }
}
