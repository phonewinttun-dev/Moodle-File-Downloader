# Moodle Course Content Downloader

A sleek, robust, and concurrent terminal-based download manager and monitor for Moodle course contents. Built specifically to tackle the painful manual process of downloading files from UCSY Moodle.

---

## 📖 Overview

### The Pain Point
Downloading course files from Moodle can be a tedious and exhausting chore. To get all materials for a single course, a student typically has to:
1. Click into the course.
2. Click through multiple sections.
3. Click individually on every single file resource to trigger a download.
4. Click into folders, then click to download files inside them.
5. Manually organize all these downloads from the default "Downloads" folder into structured directories.

This repetitive clicking results in disorganized folders and hours of wasted time.

This script was **"vibe coded"** using advanced AI tools to automate this entire flow. By combining a scraping backend with a beautiful, rich terminal-based dashboard, this tool does the heavy lifting for you. It crawls your Moodle course, extracts resources and files (even resolving embedded HTML wrapper redirects), and saves them into structured folders named by **Course Code**, **Course Title**, **Section**, and **Folder Name**.

---

## ✨ Features

- **Interactive Terminal User Interface (TUI):** A beautiful interface with live progress bars, real-time worker logs, and statistics powered by `rich`.
- **Parallel Downloads:** Powered by a thread-pool executor to download multiple files concurrently.
- **Dual Operating Modes:**
  - **Interactive Mode:** Simply run the script and follow the step-by-step TUI prompts to log in, select sections, and choose download settings.
  - **CLI / Automated Mode:** Pass arguments to run headless, perfect for scheduling cron jobs or automated scripts.
- **Smart Session Caching:** Saves your Moodle login session cookies locally for up to 7 days, so you don't have to type your password every single run.
- **Intelligent Embedded File Resolution:** Resolves Moodle's wrapper page redirect links to fetch actual PDFs/slides/documents directly.
- **Automatic Folder Structuring:** Saves files under a clean hierarchy:
  ```text
  moodle_downloads/
  └── [MODULE_CODE] [Course Title]/
      ├── Section 1/
      │   ├── Slide1.pdf
      │   └── Lab1.pdf
      └── Section 2/
          └── Folder Name/
              ├── Document1.docx
              └── Document2.docx
  ```
- **Idempotence & Skip Detection:** Checks if files already exist locally and matches their size prior to downloading to avoid redundant network usage.
- **Active Monitoring Mode:** Set an interval to run the downloader periodically, keeping your local course folder automatically in sync with Moodle.

---

## 🛠️ Project Setup & Installation

### Prerequisites
- Python 3.10 or higher is highly recommended.
- Access to your Moodle account (UCSY Moodle by default, but customizable via course URL).

### Installation Steps

1. **Clone the Repository:**
   ```bash
   git clone https://github.com/phonewinttun-dev/moodle_downloader.git
   cd moodle_downloader
   ```

2. **Create a Virtual Environment (Recommended):**
   ```bash
   python -m venv venv
   # On Windows:
   venv\Scripts\activate
   # On macOS/Linux:
   source venv/bin/activate
   ```

3. **Install Dependencies:**
   Install the required external Python libraries:
   ```bash
   pip install requests beautifulsoup4 InquirerPy rich
   ```

---

## 🚀 How to Run

Navigate to the root directory where `moodle_downloader.py` exists.

### 1. Interactive Mode
Simply run the script with no arguments:
```bash
python moodle_downloader.py
```
This opens the main menu where you can choose to download course content. You will be prompted to:
- Enter the course URL (e.g. `https://moodle.ucsy.edu.mm/course/view.php?id=XYZ` or just the ID `XYZ`).
- Choose authentication method (Email & Password, or copy-paste your browser's `MoodleSession` cookie).
- Select which course sections you want to download.
- Decide whether to download in parallel.

### 2. CLI / Non-Interactive Mode
To skip prompts or run the tool inside scripts, specify arguments:

| Argument | Description |
| :--- | :--- |
| `-u`, `--url` | The target Moodle course URL or course ID. |
| `-c`, `--cookie` | Provide a valid `MoodleSession` cookie value directly. |
| `--username` | Moodle username/email. |
| `--password` | Moodle password. |
| `-o`, `--output` | Destination folder (default: `./moodle_downloads`). |
| `-w`, `--workers` | Number of concurrent download workers (default: 4). |
| `-i`, `--interval` | Check interval in minutes. If provided, script stays active and periodically re-checks for updates. |
| `-y`, `--all` | Automatically select all course sections (bypass TUI selection menu). |

#### Example: Download using Credentials
```bash
python moodle_downloader.py -u 123 --username student@ucsy.edu.mm --password mypassword -y
```

#### Example: Download using MoodleSession Cookie
```bash
python moodle_downloader.py -u 123 -c "your_cookie_value_here" -y -w 6
```

#### Example: Active Monitoring Mode (Checks every 30 minutes)
```bash
python moodle_downloader.py -u 123 -c "your_cookie_value_here" -y -i 30 -o "D:/Moodle_Backup"
```

---

## 🔍 How It Works (Technical Overview)

1. **Authentication:**
   - Standard logging POSTs username, password, and logintoken parsed from the index page.
   - Successful sessions are cached in a hidden file `.moodle_cookie.json` for seamless runs.
2. **Page Scraping:**
   - Uses `BeautifulSoup` to parse elements with resource identifiers matching `/mod/resource/` and `/mod/folder/`.
   - Parses course headings and cleans up invalid OS characters to build file paths.
3. **Download Pipeline:**
   - Tasks are queued and fed into a thread-safe workflow managed by a [ThreadPoolExecutor](https://docs.python.org/3/library/concurrent.futures.html#threadpoolexecutor).
   - If a resource points to an embedded HTML wrapper (e.g., embedded PDF viewer), the scraper dynamically inspects the wrapper DOM to find the direct file download URL.
4. **TUI Render Engine:**
   - Utilizes `rich.Live` layout updating at 5 frames per second to show concurrent streams, download progress, speeds, ETAs, and recent activity logs.
