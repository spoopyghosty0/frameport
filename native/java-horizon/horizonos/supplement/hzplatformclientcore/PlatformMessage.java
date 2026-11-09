package horizonos.supplement.hzplatformclientcore;

/**
 * One answer HorizonPlatformCore.popMessage hands back. The SDK reads these public fields by name and JNI type
 * (requestId J, statusCode I, statusMessage and data String, cookie I); statusCode 0 = success.
 */
public final class PlatformMessage {
    public final long requestId;
    public final int statusCode;
    public final String statusMessage;
    public final String data;
    public final int cookie;

    PlatformMessage(long requestId, int statusCode, String statusMessage, String data, int cookie) {
        this.requestId = requestId;
        this.statusCode = statusCode;
        this.statusMessage = statusMessage;
        this.data = data;
        this.cookie = cookie;
    }
}
