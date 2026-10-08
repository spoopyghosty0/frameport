# Compatibility

The built-in catalog has tested settings for games, each marked as working, working with known issues, or not
running on the Frame: see the [list of tested games](GAMES.md) (recipes in [catalog/games](../catalog/games)).
Other games get suggested patches from detection rules; each suggestion states its reason, and every patch can be
switched on or off under **Customize**: described in plain words, with **Show technical details** for the exact
effect of each patch.

Tried an untested game? Its page asks how it runs; **Share working recipe…** opens a prefilled GitHub issue so your
recipe can join the built-in catalog for everyone.

![Patches](images/patches.png)

| Kind of app | On the Steam Frame |
|---|---|
| Meta Quest games (APK) | Translated to OpenXR (OVRPort) and patched for the Frame; run in Valve's Android runtime (Lepton) |
| Other Android VR apps using OpenXR (e.g. Pico builds) | Translated the same way; the other headset's own extensions and store services aren't available |
| Ordinary Android apps and games (no VR) | Installed unchanged and shown as a flat window in the headset |
| PC VR games (Windows; OpenXR, SteamVR or Oculus) | Run through Proton on the Frame (experimental), or on a Windows PC with SteamVR and streamed to the Frame; Oculus-only games use Revive |
| Can't run | 32-bit-only or x86-only APKs, Pico/HTC Wave SDK apps, Android XR apps, and games that check an Oculus licence (they need the Oculus app on a PC) |

Automated launch tests confirm that a game starts; visuals can only be checked in the headset.

![Steam Frame](images/frame.png)

The **Files** tab manages files on the Frame: upload videos, documents, mods or saves from the computer (buttons or
drag-and-drop), download, rename and delete (one entry or a selection), in the shared folders every game sees or in
one game's own storage.

![Files](images/files.png)
