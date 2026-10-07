# Screenshots

Images used by the main `README.md` and by `docs/CONFIGURATION.md`. Filenames are referenced directly, so keep them stable. `gui/tests/test_docs_images.py` checks that every image the docs point at exists, and that nothing here has stopped being used.

Retaken for 2.0. The wizard and Settings shots are cropped to the card; the dashboard, History and Stats shots show the whole window.

| File | Used for |
|------|----------|
| `Setup_Wizard.png` | README · Setup → welcome |
| `Audio_Device_Selection.png` | README · Setup → microphone |
| `Calibration_Method_Selection.png` | README · Setup → calibration (auto vs manual) |
| `Noise_Floor_Auto_Calibration.png` | README · Setup → auto-calibrate, noise floor |
| `Music_Level_Auto_Calibration.png` | README · Setup → auto-calibrate, capture a song |
| `Manual_Threshold_Calibration.png` | README · Setup → manual threshold |
| `Connection_Selection.png` | README · Setup → Home Assistant step |
| `Blank_Dashboard.png` | README · Usage → dashboard, idle |
| `Dashboard_with_history_and_now_playing.png` | README · Usage → dashboard, now playing |
| `History_Page.png` | README · Usage → History |
| `Stats_Page.png` | README · Usage → Stats |
| `Settings_LastFM.png` | README · Usage → Last.fm; CONFIGURATION · Last.fm |
| `Home_Assistant_Integration.png` | README · Usage → In Home Assistant |
| `Settings_Audio.png` | CONFIGURATION · Audio |
| `Settings_Backup_Recognizer.png` | CONFIGURATION · Backup recognizers |
| `Settings_Diagnostics.png` | CONFIGURATION · Diagnostics |
| `Settings_Hardware.png` | CONFIGURATION · Hardware |
| `Settings_Home_Assistant.png` | CONFIGURATION · Home Assistant discovery |

Also: `spinsense-logo.png` (README header) and `behringer-ufo202.webp` (recommended audio interface, rendered small and right-aligned in Installation).

## Still wanted (optional)

- A **Home Assistant `media_player` card** showing a track — the integration payoff.
- A wizard **"Done"/finish** screen.
