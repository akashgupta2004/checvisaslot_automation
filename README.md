# CheckVisaStart Isolated Codespace

This repository contains the isolated GUI and CLI automation workspace for the **CheckVisaSlots** contribution flow. It automates logging in to the US Visa Scheduling portal, bypassing the Cloudflare waiting room, answering security questions, navigating to the OFC scheduling page, and rotating cities to check visa slot availability.

---

## 📂 Project Structure

```text
checkvisastart_codespace/
├── extensions/
│   └── checkvisaslots/     # Unpacked CheckVisaSlots Chrome Extension
├── src/
│   ├── main.py             # Main sequential account scheduler
│   ├── auth/
│   │   ├── browser.py      # Chrome debugger and CDP connection setup
│   │   ├── captcha.py      # Captcha solving dispatcher (FastCaptcha / Manual)
│   │   ├── cdp_client.py   # Portal navigation checks
│   │   ├── login.py        # Credentials typing & form submission logic
│   │   └── security.py     # Answering security questions from accounts.json
│   ├── core/
│   │   ├── contribution_runner.py  # OFC city rotation and status updates
│   │   └── login_runner.py         # Automated login subprocess runner
│   └── utils/
│       ├── common.py       # Humanized delays, typing, and clicking
│       └── slack.py        # Slack notifications integration
├── state/                  # Output state files for each account session
├── profiles/               # Isolated Chrome user data profiles per account
├── logs/                   # Subprocess stdout and execution logs
├── accounts.json           # User credentials and security answers
├── settings.json           # Global running preferences (rotations, cooldowns, etc.)
├── gui.py                  # Tkinter-based management dashboard
├── run_gui.bat             # Batch launcher for the GUI
├── run_gui.ps1             # PowerShell launcher for the GUI
└── run_contribution.bat    # Batch launcher for CLI-only execution
```

---

## 🛠️ Prerequisites

1. **Python 3.12+** must be installed on your system.
2. **Google Chrome** is recommended (if using your local installation, ensure the path matches `C:\Program Files\Google\Chrome\Application\chrome.exe`).
3. **Playwright** browser binaries must be installed.

---

## 🚀 Quick Start & Setup

The launcher scripts automatically handle environment activation and dependency installation.

### Method 1: Using the GUI (Recommended)
Double-click **`run_gui.bat`** (Windows Command Prompt) or run **`.\run_gui.ps1`** (PowerShell). 

This launcher will:
1. Boot the Tkinter configuration dashboard.
2. Allow you to add, edit, or delete accounts.
3. Manage settings such as dwell times, rotation limits, and cooldowns.
4. Launch the scheduler and display real-time stdout logs in the text area.

### Method 2: Command Line (CLI Scheduler)
Run **`run_contribution.bat`** from your terminal. 

This script will:
1. Install/upgrade pip and run `pip install -r requirements.txt`.
2. Ensure Playwright Chrome binaries are installed (`playwright install chromium`).
3. Start the scheduler in CLI-only mode.

To run manually using python directly:
```powershell
# Run the GUI dashboard
python gui.py

# Run the sequential CLI scheduler
python -m src.main --dwell-seconds 12 --max-rotations 15 --cooldown-seconds 3600
```

---

## ⚙️ Configuration Files

### 1. `accounts.json`
Stores account credentials, target cities, and security questions.
```json
[
  {
    "customer_name": "JohnDoe",
    "username": "johndoe_visa",
    "password": "SecretPassword123!",
    "contributionCities": [
      "NEW DELHI",
      "MUMBAI",
      "HYDERABAD",
      "CHENNAI",
      "KOLKATA"
    ],
    "ofcCities": [
      "NEW DELHI",
      "MUMBAI",
      "HYDERABAD",
      "CHENNAI",
      "KOLKATA"
    ],
    "security_questions": {
      "food": "Pizza",
      "car": "Toyota",
      "hero": "Spider-Man"
    }
  }
]
```
> [!NOTE]
> The keys in `"security_questions"` (e.g., `"food"`, `"car"`) are case-insensitive substrings matched against the question label text displayed on the portal.

### 2. `settings.json`
Stores scheduler running parameters.
```json
{
  "dwell_seconds": 12,
  "cycles": 0,
  "max_rotations": 15,
  "cooldown_minutes": 60,
  "min_gap_seconds": 15,
  "max_gap_seconds": 20,
  "browser_exe": "",
  "extension_path": "C:\\Users\\...\\checkvisastart_codespace\\extensions\\checkvisaslots"
}
```

---

## 🧠 Under The Hood: How it Works

1. **Session Reuse Optimization**: 
   When the scheduler starts an account, it checks if a saved profile exists in `profiles/chrome_profile_<customer>`. If found, it opens Chrome directly to the OFC page. If the session is still authenticated, it skips the login stage entirely.
2. **Cloudflare Waiting Room Handling**: 
   The script checks for Cloudflare challenge checkmarks or Turnstile wrappers and clicks them automatically using human-like mouse movements.
3. **Answering Security Questions**: 
   The bot extracts the questions from the page, matches them with the corresponding answers from `accounts.json`, and types them character-by-character with randomized keystroke intervals.
4. **Anti-Fingerprinting Bypass**: 
   Chrome Extensions with dynamic URLs (Manifest V3 security) generate a dynamic UUID per session, making `options.html` crash when opened directly via UUID. The script bypasses this by briefly loading the web-accessible `popup.html` first, executing `chrome.runtime.id` to retrieve the real, static extension ID, and then opening and configuring the Options page (hot-loading the API key `4XYRAN`).
5. **Dwell and Cooldown Rotation**: 
   For each city, the bot selects it, waits for the slot calendars to load (dwell time), increments the rotation counter, and waits a randomized gap (e.g., 15–20s) to behave like a natural human user. Once the account reaches its rotation limit, it enters a cooldown phase and the next account in `accounts.json` is initiated.

---

## 📈 Logs & State Monitoring

* **Logs**: Detailed execution logs for the subprocesses are written to `logs/latest_run.log`.
* **State**: Current status (e.g., `city_checked`, `cooldown`, `starting`, `rotation_limit_reached`) is written in JSON format to `state/scheduler_state_<customer>.json` and `state/contribution_state_<customer>.json`. These are polled by the GUI to update progress.
