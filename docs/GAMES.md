# Tested games

Games tested on the Steam Frame with FramePort's recipes. Games not listed here may work too: FramePort suggests patches for them, and a working recipe can be shared from the app (**Share working recipe…**).
Tested a game? [Share a working config](https://github.com/spoopyghosty0/frameport/issues/new?template=working-config.yml) or [report a problem](https://github.com/spoopyghosty0/frameport/issues/new?template=bug-report.yml) (in the app: the game's **…** menu does both and fills in the details).
Generated from [catalog/games](../catalog/games) by `scripts/compat_list.py`.

| Game | Platform | Status | Notes |
|---|---|---|---|
| 4XVR Video Player | Quest | ✅ Works |  |
| Accounting+ | Quest | ✅ Works |  |
| AllInOneSports | Quest | ✅ Works |  |
| Asgard's Wrath 2 | Quest | ✅ Works |  |
| BAM | Quest | ✅ Works |  |
| BARTENDER VR SIMULATOR | Quest | ✅ Works |  |
| Batman: Arkham Shadow | Quest | ✅ Works |  |
| BattleGlide | Quest | ✅ Works |  |
| BattleSisters | Quest | ✅ Works |  |
| Beat Saber | Quest | ✅ Works |  |
| Beat Saber | Quest | ✅ Works |  |
| Beat Saber (co-existence build) | Quest | ✅ Works |  |
| Blade & Sorcery: Nomad | Quest | ✅ Works |  |
| BodyCombat | Quest | ✅ Works |  |
| Carve Snowboarding | Quest | ✅ Works |  |
| Cook-Out | Quest | ✅ Works |  |
| Creed | Quest | ✅ Works |  |
| Demeter | Quest | ✅ Works |  |
| Dinosaur Island | Quest | ✅ Works |  |
| Espire 2 | Quest | ✅ Works |  |
| Genotype | Quest | ✅ Works |  |
| H.U.N.T | Quest | ✅ Works |  |
| I Am Cat | Quest | ✅ Works |  |
| I Am Monkey | Quest | ✅ Works |  |
| In Death: Unchained | Quest | ✅ Works |  |
| Into The Radius 2 | Quest | ✅ Works |  |
| Job Simulator | Quest | ✅ Works |  |
| Jurassic World Aftermath Collection | Quest | ✅ Works |  |
| Keep Talking and Nobody Explodes | Quest | ✅ Works |  |
| Lambda1VR | Quest | ✅ Works |  |
| LEGO® Bricktales | Quest | ✅ Works |  |
| Lucky's Tale | Quest | ✅ Works |  |
| Marvel's Deadpool VR | Quest | ✅ Works |  |
| Marvel's Iron Man VR | Quest | ✅ Works |  |
| Medieval Dynasty New Settlement | Quest | ✅ Works |  |
| Metro Awakening | Quest | ✅ Works |  |
| Mobile Suit Gundam: Silver Phantom | Quest | ✅ Works |  |
| Nano | Quest | ✅ Works |  |
| NEX Player | Quest | ✅ Works |  |
| NOPE CHALLENGE | Quest | ✅ Works |  |
| palazzo_santacruz | Quest | ✅ Works |  |
| Path of the Warrior | Quest | ✅ Works |  |
| PowerWash Simulator VR | Quest | ✅ Works |  |
| QuestCraft | Quest | ✅ Works |  |
| Rick and Morty: Virtual Rick-ality | PC VR | ✅ Works |  |
| Riven | Quest | ✅ Works |  |
| Robo Recall | Quest | ✅ Works |  |
| Sniper Elite VR: Winter Warrior | Quest | ✅ Works |  |
| Space Pirate Trainer Quest | Quest | ✅ Works |  |
| Stremio | Quest | ✅ Works |  |
| SUPERHOT VR | PC VR | ✅ Works |  |
| SUPERHOT VR | Quest | ✅ Works |  |
| TetrisEffect | Quest | ✅ Works |  |
| The Boys VR | Quest | ✅ Works |  |
| The Climb 2 | Quest | ✅ Works |  |
| The Room VR | Quest | ✅ Works |  |
| Toy Master | Quest | ✅ Works |  |
| Under Cover | Quest | ✅ Works |  |
| VR4 | Quest | ✅ Works |  |
| Wallace & Gromit in The Grand Getaway | Quest | ✅ Works |  |
| Waltz of the Wizard: Extended Edition | Quest | ✅ Works |  |
| Wander | Quest | ✅ Works |  |
| Arcsmith | Quest | ⚠️ Works with issues | Right eye distorts during movement (unresolved; swap, tracking, Valve layers, depth and pacing ruled out). |
| Assassin's Creed Nexus | Quest | ⚠️ Works with issues | Some launch warning text is still upside down; the rest of the UI is fixed by flip emulation. |
| BONELAB | Quest | ⚠️ Works with issues | Build 1.2068 plays (head tracking and controls work with frame.unity_user_presence); the newer build 1.2974 crashes at start (Vulkan), no fix yet. |
| Myst | Quest | ⚠️ Works with issues | Minor graphical glitches on some objects. |
| Phantom: Covert Ops | Quest | ⚠️ Works with issues | DLC/store button crashes (no Meta store). |
| Pinball FX VR | Quest | ⚠️ Works with issues | Plays; mixed reality mode not working yet. |
| Silhouette | Quest | ⚠️ Works with issues | Hand-tracking game; the Frame synthesizes hands from controllers, so it is janky. |
| Time Stall | Quest | ⚠️ Works with issues | Both eyes distort during movement (unresolved). |
| Vader Immortal: Episode I | Quest | ⚠️ Works with issues | Starts in VR and plays the intro, then stays on the loading card (Vader's portrait with a progress bar). |
| WiiCompiled VR | Quest | ⚠️ Works with issues | To add a game: in FramePort's Files tab, upload your .wcgame file to this game's storage, folder Android/data/org.wiicompiled.quest/files/WiiCompiledOpenXRVR… |
| BlazeRush | Quest | ❌ Doesn't run | Starts and reaches the menu room, but the room shows no controllers and ignores all input (it all reaches the game); no fix yet. |
| Espire 1: VR Operative (Quest Edition) | Quest | ❌ Doesn't run | Mesa GL driver crash during texture upload. |
| HITMAN 3 VR: Reloaded | Quest | ❌ Doesn't run | Vulkan driver crash (freedreno), even without Valve layers. |
| Journey of the Gods | Quest | ❌ Doesn't run | 32-bit only; the Frame has no AArch32. |
| Roblox | Quest | ❌ Doesn't run | Crashes on its first VR frame on the Frame. |
| Shadow Point | Quest | ❌ Doesn't run | 32-bit only; the Frame has no AArch32. |
| Sniper Elite VR | Quest | ❌ Doesn't run | GPU hang (zink: DEVICE LOST) even with MSAA off. |
| Sports Scramble (Santa Cruz) | Quest | ❌ Doesn't run | 32-bit only; the Frame has no AArch32. |
