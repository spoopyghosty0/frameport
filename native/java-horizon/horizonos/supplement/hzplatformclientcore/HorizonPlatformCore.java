package horizonos.supplement.hzplatformclientcore;

import android.content.Context;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.util.ArrayDeque;
import java.util.HashMap;
import java.util.HashSet;
import java.util.Iterator;
import java.util.Map;
import java.util.Set;

/**
 * A local stand-in for Horizon OS's hzplatformclientcore supplement (frame.horizon_platform_stub), which Meta's
 * Horizon Platform SDK (com.meta.xr.sdk.platform 2xx) creates in Oculus.Platform AndroidClient.Initialize/InitAsync.
 * The Frame has no such component, so without this class the SDK's start fails (ClassNotFoundException).
 *
 * The SDK talks to it with JSON requests named by module and request name (makeRequest) and collects the answers
 * with getMessageCount/popMessage on the global session id. Which requests get a success answer is not decided
 * here: the patch writes assets/frameport/horizon-platform.json at build time (`answers`: "module/name" -> data,
 * optionally only when the request's fields `match`; `error_data`: the data an error answer carries for requests
 * whose result type isn't an object; `licence`: modules and requests that are licence or account checks). Every
 * request without an answer, licence and account checks always, gets an error status (12, RequestNotSupported):
 * nothing here ever claims a licence, a purchase or a signed-in Meta account.
 */
public final class HorizonPlatformCore {
    static final String TAG = "FramePort";
    static final String CONFIG = "frameport/horizon-platform.json";
    static final long GLOBAL_SESSION = 1;
    static final int NOT_SUPPORTED = 12;  // HorizonStatus RequestNotSupported

    private final ArrayDeque<PlatformMessage> responses = new ArrayDeque<PlatformMessage>();
    private final Set<Long> sessions = new HashSet<Long>();
    private final Set<String> logged = new HashSet<String>();
    private final Map<String, JSONObject> answers = new HashMap<String, JSONObject>();
    private final Map<String, String> errorData = new HashMap<String, String>();
    private final Set<String> licence = new HashSet<String>();
    private long nextId = 1;
    private boolean ready;

    public HorizonPlatformCore() {
    }

    public synchronized void setup(AndroidHorizonPlatformConfig config) {
        if (ready) {
            return;
        }
        ready = true;
        try {
            load(read(config == null ? null : config.context));
            Log.i(TAG, "Horizon platform stand-in ready (" + answers.size() + " local answers; licence and account "
                    + "checks are not answered)");
        } catch (Throwable t) {
            // no table: every request gets the error answer
            Log.w(TAG, "Horizon platform stand-in: no answer table (" + t + "): every request answers an error");
        }
    }

    public long getGlobalSessionId() {
        return GLOBAL_SESSION;
    }

    public synchronized long makeRequest(String module, String name, int apiVersion, String json, int cookie) {
        long id = nextId++;
        responses.add(answer(module, name, json, id, cookie));
        return id;
    }

    /** Notification subscriptions: a session id that never receives a message (nothing here sends notifications). */
    public synchronized long makeSession(String module, String name, int apiVersion, String json, int cookie) {
        long id = ++nextId + GLOBAL_SESSION;
        sessions.add(id);
        note(module + "/" + name, "notifications: none will come");
        return id;
    }

    public synchronized void stopSession(long session) {
        sessions.remove(session);
    }

    public synchronized int getMessageCount(long session) {
        return session == GLOBAL_SESSION ? responses.size() : 0;
    }

    /** The oldest answer of the global session, or null when there is none (the SDK only asks after a count > 0). */
    public synchronized PlatformMessage popMessage(long session, boolean yield) {
        return session == GLOBAL_SESSION ? responses.poll() : null;
    }

    PlatformMessage answer(String module, String name, String json, long id, int cookie) {
        String key = module + "/" + name;
        JSONObject entry = answers.get(key);
        if (entry != null && matches(entry.optJSONObject("match"), json)) {
            note(key, "answered locally");
            return new PlatformMessage(id, 0, "", entry.optString("data", "{}"), cookie);
        }
        boolean isLicence = licence.contains(key) || licence.contains(module + "/*");
        note(key, isLicence ? "not answered (licence or account check)" : "not available (error answer)");
        String message = isLicence
                ? "FramePort: licence and account checks are not answered on this device (" + key + ")"
                : "FramePort: " + key + " is not available on this device (no Meta platform service)";
        String data = errorData.containsKey(key) ? errorData.get(key) : "{}";
        return new PlatformMessage(id, NOT_SUPPORTED, message, data, cookie);
    }

    static boolean matches(JSONObject match, String json) {
        if (match == null) {
            return true;
        }
        try {
            JSONObject request = new JSONObject(json == null || json.length() == 0 ? "{}" : json);
            for (Iterator<String> it = match.keys(); it.hasNext(); ) {
                String field = it.next();
                if (!match.optString(field).equals(request.optString(field, null))) {
                    return false;
                }
            }
            return true;
        } catch (Throwable t) {
            return false;
        }
    }

    void load(String text) throws Exception {
        JSONObject root = new JSONObject(text);
        JSONObject a = root.optJSONObject("answers");
        if (a != null) {
            for (Iterator<String> it = a.keys(); it.hasNext(); ) {
                String key = it.next();
                answers.put(key, a.getJSONObject(key));
            }
        }
        JSONObject e = root.optJSONObject("error_data");
        if (e != null) {
            for (Iterator<String> it = e.keys(); it.hasNext(); ) {
                String key = it.next();
                errorData.put(key, e.getString(key));
            }
        }
        JSONArray l = root.optJSONArray("licence");
        if (l != null) {
            for (int i = 0; i < l.length(); i++) {
                licence.add(l.getString(i));
            }
        }
    }

    static String read(Context context) throws Exception {
        InputStream in = context.getAssets().open(CONFIG);
        try {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buf = new byte[8192];
            for (int n; (n = in.read(buf)) > 0; ) {
                out.write(buf, 0, n);
            }
            return new String(out.toByteArray(), "UTF-8");
        } finally {
            in.close();
        }
    }

    private void note(String key, String what) {
        if (logged.add(key)) {  // once per request kind (no request data: it can carry user content)
            Log.i(TAG, "Horizon platform: " + key + " -> " + what);
        }
    }
}
