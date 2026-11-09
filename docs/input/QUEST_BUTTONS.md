# Quest controller mode: contract (`quest_buttons`)

This file is the contract between the native (FrameBridge) and app/docs halves of the feature. The mapping itself
lives in [`quest_buttons.json`](quest_buttons.json); everything else follows it.

## Problem
Quest games suggest bindings only for `/interaction_profiles/oculus/touch_controller`. The Steam Frame controllers
are two gamepad halves (Valve: https://partner.steamgames.com/doc/steamhardware/steamframe/input):
- left: `dpad_up|dpad_down|dpad_left|dpad_right` (click, touch), `view` (click, touch), `system`, `bumper`,
  `squeeze` (click, touch, value), `trigger` (click, touch, value), `thumbstick` (vector, x, y, click, touch),
  `grip/pose`, `aim/pose`, `output/haptic`
- right: `a|b|x|y` (click, touch), `menu` (click, touch), `system`, `bumper`, and the same squeeze/trigger/thumbstick/
  poses/haptic.

SteamVR uses a game's Frame Controller binding first; without one it remaps Touch: Touch B/Y -> the three top buttons
(left d-pad left/up/right, right X/Y/B), Touch A/X -> the bottom one, Touch menu -> View (mode `0` in the JSON).

## Setting
FrameBridge adapter setting `quest_buttons` (int, read from settings like every other key; numbers only):
- `0` = off (default): FrameBridge does nothing new, SteamVR's remap applies.
- `1` = Mirror, `2` = Quest pair, `3` = One hand (GitHub discussion #570): see `modes` in the JSON.

Library setting `input.quest_buttons` (0-3, default 0) = the global default, used at install for games whose recipe
doesn't name `adapter.quest_buttons`.

## Native behaviour (FrameBridge)
1. At `xrCreateInstance`, when `quest_buttons` != 0 and the runtime lists `XR_VALVE_frame_controller_interaction`,
   add it to the enabled extensions (the adapter already rewrites this list). Not listed -> log and stay off.
2. When the game's suggestion for the Touch profile succeeds (directly, or through `profile_remap`'s
   `remap_to_touch`), suggest a translated set for `/interaction_profiles/valve/frame_controller_valve`:
   - each binding `/user/hand/<hand>/input/<touch input>[/<component>]` maps by the JSON: `common` 1:1, button
     `x|y|a|b` + `/click` or `/touch` -> each listed Frame button + the same component, `menu/click` -> listed
     Frame button + `/click`; `output/haptic` 1:1; anything in `dropped` or unknown is left out.
   - a later Touch suggestion by the same game replaces the Frame set (suggest again; the last call per profile
     wins in OpenXR).
   - a failed Frame suggestion is logged and ignored: the game's own call result is never changed.
   - One hand (mode 3, `native/adapter/quest_onehand.c`): a mode's `other_hand` and `dpad_stick` entries can't be
     bindings. Touch X/Y are left-hand inputs; a binding on `/user/hand/right` feeds the action's right subaction
     (Unity's primarybutton = X on the left, A on the right: Frame X would read as A) and leaves the left one empty,
     and no binding turns d-pad buttons into a stick vector. So when the game's Touch set binds left `x`, `y`
     (bare, click, touch) or the left `thumbstick` (vector, x, y), FrameBridge creates its own action set
     `framebridge_one_hand` (eight boolean actions without subaction paths: right `x|y` click + touch, left
     `dpad_up|down|left|right` click), appends their bindings to the generated Frame suggestion, adds the set to
     the game's `xrAttachSessionActionSets` and every `xrSyncActions`, reads it after each sync and answers the
     game's queries for the left hand (or with `XR_NULL_PATH`): `xrGetActionStateBoolean`/`Float` of left-X/Y
     actions = the game's state OR the right X/Y (float 1.0), `isActive` set when FrameBridge's action is;
     `xrGetActionStateVector2f` / `Float` of left-thumbstick actions = the d-pad direction (diagonals normalised
     to length 1) while a d-pad button is held, else the real stick. `changedSinceLastSync`/`lastChangeTime`
     describe the answer given while FrameBridge's input contributes (or did at the previous sync). Queries for
     the right hand, other sessions and every mode but 3 are untouched. A failed set creation or attach is logged
     and the game's own sets are attached alone (plain bindings, left X/Y unbound).
3. `xrGetCurrentInteractionProfile`: with `quest_buttons` != 0, a `/valve/` answer is reported as Touch even when
   `controller_fix` is off.
4. Log lines (logcat tag of the adapter):
   - `quest_buttons: extension XR_VALVE_frame_controller_interaction enabled` / `... not offered by the runtime`
   - `quest_buttons: frame_controller bindings (mode N, K bindings, D dropped): <XrResult>`
   - with `input_diag=1`: `input_diag: quest_buttons <touch path> -> <frame path>` per added binding
     (`FrameBridge -> <frame path>` for the one-hand set's own bindings).
   - mode 3: `quest_buttons: one hand: N game actions answered from the right X/Y and the left d-pad`,
     `quest_buttons: one hand: FrameBridge's action set attached` (or `... attaching ... failed (<XrResult>) ...`).

## Owners
- native stream: `native/adapter/*`, `tests/test_quest_buttons.py`, rebuilt adapter artifact.
- app stream: `src/frameport/**` (settings, install fallback, UI, help), `docs/**` except this file and the JSON,
  `scripts/controller_diagram.py`, `README.md` link, `agent/frameport_agent.py` comment only, Python tests.
