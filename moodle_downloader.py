import os
import re
import sys
import time
import json
import argparse
import getpass
import threading
import collections
import urllib.parse
import posixpath
import concurrent.futures

import requests
from bs4 import BeautifulSoup
from InquirerPy import inquirer
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.progress import Progress, BarColumn, TextColumn, DownloadColumn, TransferSpeedColumn, TimeRemainingColumn
from rich.layout import Layout
from rich import box

# --- Styled Progress Column subclasses for visual richness ---
class StyledDownloadColumn(DownloadColumn):
    def render(self, task):
        text = super().render(task)
        if isinstance(text, Text):
            text.style = "bold yellow"
        else:
            text = Text(str(text), style="bold yellow")
        return text

class StyledTransferSpeedColumn(TransferSpeedColumn):
    def render(self, task):
        text = super().render(task)
        if isinstance(text, Text):
            text.style = "bold magenta"
        else:
            text = Text(str(text), style="bold magenta")
        return text

class StyledTimeRemainingColumn(TimeRemainingColumn):
    def render(self, task):
        text = super().render(task)
        if isinstance(text, Text):
            text.style = "bold blue"
        else:
            text = Text(str(text), style="bold blue")
        return text


# Ensure terminal outputs UTF-8 correctly
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except AttributeError:
    pass

# Initialize console
console = Console()

BANNER = r"""
===========================================================
  __  __                 _ _         _____   _
 |  \/  |               | | |       |  __ \ | |
 | \  / | ___   ___   __| | | ___   | |  | || |
 | |\/| |/ _ \ / _ \ / _` | |/ _ \  | |  | || |
 | |  | | (_) | (_) | (_| | |  __/  | |__| || |____
 |_|  |_|\___/ \___/ \__,_|_|\___|  |_____/ |______|

 Moodle Course Content Downloader (UCSY Edition)
===========================================================
"""

# Cookie cache file setup
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
COOKIE_CACHE_FILE = os.path.join(SCRIPT_DIR, ".moodle_cookie.json")

# --- Cookie Cache Utility Functions ---

def load_cached_session(course_url: str) -> dict | None:
    """Loads session details from local cache if matches domain and not expired."""
    if not os.path.exists(COOKIE_CACHE_FILE):
        return None
    try:
        with open(COOKIE_CACHE_FILE, 'r', encoding='utf-8') as f:
            cache = json.load(f)

        parsed_course = urllib.parse.urlparse(course_url)
        parsed_cache = urllib.parse.urlparse(cache.get("course_url", ""))

        # Check if the cache matches base domain and course URL, and is not expired
        if parsed_course.netloc == parsed_cache.netloc:
            if cache.get("expires_at", 0) > time.time():
                return cache
    except Exception:
        pass
    return None

def save_session_cache(course_url: str, cookies: dict, username: str | None = None):
    """Saves session cookies to local cache file (valid for 7 days)."""
    try:
        cache = {
            "course_url": course_url,
            "username": username,
            "cookies": cookies,
            "expires_at": time.time() + 7 * 24 * 3600
        }
        with open(COOKIE_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(cache, f, indent=2)
    except Exception:
        pass

def clear_session_cache():
    """Removes cached cookie file."""
    if os.path.exists(COOKIE_CACHE_FILE):
        try:
            os.remove(COOKIE_CACHE_FILE)
        except Exception:
            pass

# --- Network & Scraper Layer ---

class MoodleSession:
    """Wrapper around requests.Session managing tokens, connection pooling, and credentials."""
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip('/')
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        })

    def inject_cookies(self, cookies_dict: dict):
        """Injects dictionary of cookies into session."""
        parsed_domain = urllib.parse.urlparse(self.base_url).netloc
        for name, value in cookies_dict.items():
            self.session.cookies.set(name, value, domain=parsed_domain, path='/')

    def set_cookie(self, value: str):
        """Sets MoodleSession cookie directly."""
        parsed_domain = urllib.parse.urlparse(self.base_url).netloc
        self.session.cookies.set('MoodleSession', value, domain=parsed_domain, path='/')

    def login(self, username, password) -> tuple[bool, str]:
        """Performs authentication against Moodle login/index.php."""
        login_url = f"{self.base_url}/login/index.php"
        try:
            res = self.session.get(login_url, timeout=15)
            res.raise_for_status()
            html_content = res.text
        except Exception as e:
            return False, f"Failed to contact login page: {e}"

        soup = BeautifulSoup(html_content, 'html.parser')
        token_input = soup.find('input', {'name': 'logintoken'})
        logintoken = token_input['value'] if token_input else ""

        login_data = {
            'username': username,
            'password': password,
            'logintoken': logintoken
        }

        try:
            res = self.session.post(login_url, data=login_data, timeout=15)
            res.raise_for_status()
            response_html = res.text

            if "loginerrormessage" in response_html or "Invalid login" in response_html:
                return False, "Invalid username or password."

            cookies = self.session.cookies.get_dict()
            if 'MoodleSession' in cookies:
                return True, "Login successful!"
            else:
                return False, "MoodleSession cookie not found in response."
        except Exception as e:
            return False, f"Post login request failed: {e}"

    def get(self, url, **kwargs):
        """Wrapper around session.get."""
        return self.session.get(url, **kwargs)


def parse_course_page(html_content: str) -> list[dict]:
    """Parses course html content and extracts sections and activity items."""
    soup = BeautifulSoup(html_content, 'html.parser')

    # Decompose hidden elements used for screen-readers to avoid polluting titles
    for hidden in soup.find_all(class_='accesshide'):
        hidden.decompose()

    sections = []

    # Attempt list-based section parsing
    li_sections = soup.select('li.section.main, li.section.outer')
    if li_sections:
        for sec in li_sections:
            header_el = sec.select_one('.sectionname, .section-title, .section-heading, h3, h4')
            name = header_el.get_text(strip=True) if header_el else "General"

            items = []
            for a in sec.find_all('a', href=True):
                href = a['href']
                if '/mod/resource/view.php?id=' in href:
                    text = a.get_text(strip=True)
                    if href not in [item['url'] for item in items] and text:
                        items.append({'type': 'resource', 'name': text, 'url': href})
                elif '/mod/folder/view.php?id=' in href:
                    text = a.get_text(strip=True)
                    if href not in [item['url'] for item in items] and text:
                        items.append({'type': 'folder', 'name': text, 'url': href})

            if items:
                sections.append({'name': name, 'items': items})

    # Fallback to document-order matching if structure is customized
    if not sections:
        current_section = {"name": "General", "items": []}
        for el in soup.find_all(['h2', 'h3', 'h4', 'a']):
            if el.name in ['h2', 'h3', 'h4'] or any(cls in el.get('class', []) for cls in ['sectionname', 'section-title', 'section-heading']):
                header_text = el.get_text(strip=True)
                if header_text:
                    if current_section["items"]:
                        sections.append(current_section)
                    current_section = {"name": header_text, "items": []}
            elif el.name == 'a' and el.get('href'):
                href = el['href']
                text = el.get_text(strip=True)
                if not text:
                    continue
                if '/mod/resource/view.php?id=' in href:
                    if not any(item['url'] == href for item in current_section['items']):
                        current_section['items'].append({'type': 'resource', 'name': text, 'url': href})
                elif '/mod/folder/view.php?id=' in href:
                    if not any(item['url'] == href for item in current_section['items']):
                        current_section['items'].append({'type': 'folder', 'name': text, 'url': href})

        if current_section["items"] or current_section["name"] != "General":
            sections.append(current_section)

    return sections


def parse_folder_page(html_content: str) -> list[dict]:
    """Parses folder activity page to find individual file links."""
    soup = BeautifulSoup(html_content, 'html.parser')
    for hidden in soup.find_all(class_='accesshide'):
        hidden.decompose()

    files = []
    for a in soup.find_all('a', href=True):
        href = a['href']
        if 'pluginfile.php' in href and 'download_folder.php' not in href and '/mod_folder/content/' in href:
            text = a.get_text(strip=True)
            if text and not any(f['url'] == href for f in files):
                files.append({'name': text, 'url': href})
    return files


# --- File Utilities ---

def clean_filename(name: str) -> str:
    """Removes invalid OS filename characters and tidies up spacing."""
    name = re.sub(r'[\\/*?:"<>|]', '_', name)
    name = re.sub(r'\s+', ' ', name).strip()
    return name


def extract_course_info(html_content: str) -> tuple[str, str | None]:
    """Extracts course title and optional module code from course page HTML."""
    soup = BeautifulSoup(html_content, 'html.parser')
    
    # Decompose hidden elements if any in title selectors
    for hidden in soup.find_all(class_='accesshide'):
        hidden.decompose()
        
    # Try different selectors to locate the course title
    h1 = soup.select_one('.page-header-headings h1')
    if not h1:
        h1 = soup.select_one('header h1')
    if not h1:
        h1 = soup.select_one('.course-header h1')
    
    course_title = ""
    if h1:
        course_title = h1.get_text(strip=True)
        
    if not course_title:
        # Fallback to <title> tag
        title_tag = soup.title
        if title_tag and title_tag.string:
            title_str = title_tag.string.strip()
            # Clean Moodle default format: "Course: Title" or "Course: Title | SiteName"
            if "Course:" in title_str:
                course_title = title_str.split("Course:", 1)[1].strip()
            else:
                course_title = title_str
            # Remove site name suffix if present (e.g. " | UCSY Moodle" or " - UCSY")
            if " | " in course_title:
                course_title = course_title.split(" | ", 1)[0].strip()
            elif " - " in course_title:
                course_title = course_title.split(" - ", 1)[0].strip()
                
    if not course_title:
        course_title = "Unknown Course"

    # Extract module code from course_title before cleaning filename
    # e.g., "IS-101 (Software Engineering)" -> "IS-101"
    # Pattern: 1-4 letters, optional hyphen/space, 2-4 digits
    pattern = r'\b([A-Za-z]{1,4}[- ]?\d{2,4})\b'
    match = re.search(pattern, course_title)
    
    module_code = None
    if match:
        module_code = match.group(1).upper()
        
    # Now clean both values for safe directory naming
    course_title = clean_filename(course_title)
    if module_code:
        module_code = clean_filename(module_code)
        
    return course_title, module_code


# --- Thread-Safe State & UI Layout ---

active_jobs = []
active_jobs_lock = threading.Lock()

log_messages = collections.deque(maxlen=6)
log_lock = threading.Lock()

completed_files = 0
skipped_files = 0
failed_files = 0
processed_files = 0
failed_downloads = []
global_lock = threading.Lock()

worker_progresses = []


def safe_log(msg: str, style: str = None):
    """Thread-safe logging utility that updates the console log block."""
    with log_lock:
        timestamp = time.strftime('%H:%M:%S')
        if style:
            text = Text.assemble((f"[{timestamp}] ", "dim"), (msg, style))
        else:
            text = Text.assemble((f"[{timestamp}] ", "dim"), (msg))
        log_messages.append(text)


def make_dashboard_layout(start_time: float, status_text: str, processed_count: int, total_count: int, workers: int) -> Layout:
    """Generates the multi-component terminal dashboard layout."""
    # Header Panel
    elapsed = int(time.time() - start_time)
    hours, remainder = divmod(elapsed, 3600)
    minutes, seconds = divmod(remainder, 60)
    time_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    header_content = Text.assemble(
        ("UCSY Moodle Downloader\n", "bold cyan"),
        ("Session Status: ", "yellow"), (f"{status_text}  |  ", "white"),
        ("Runtime: ", "yellow"), (f"{time_str}", "white")
    )
    header = Panel(header_content, border_style="cyan", box=box.DOUBLE)

    # Active Jobs Table
    table = Table(expand=True, box=box.ROUNDED, border_style="blue")
    table.add_column("Worker", justify="center", width=10, style="bold magenta")
    table.add_column("File Name", justify="left", ratio=4, style="bold white")
    table.add_column("Destination Folder", justify="left", ratio=3, style="dim cyan")
    table.add_column("Progress / Speed / ETA", justify="left", ratio=5)

    with active_jobs_lock:
        for idx in range(workers):
            job = active_jobs[idx]
            if job:
                filename = job['filename']
                if len(filename) > 35:
                    filename = filename[:32] + "..."
                dest = job['dest']
                if len(dest) > 25:
                    dest = "..." + dest[-22:]
                table.add_row(
                    f"Worker {idx+1}",
                    Text(filename, style="bold green"),
                    Text(dest, style="cyan"),
                    worker_progresses[idx]
                )
            else:
                table.add_row(
                    f"Worker {idx+1}",
                    Text("Idle", style="dim italic"),
                    Text("-", style="dim"),
                    Text("-", style="dim")
                )

    jobs_panel = Panel(table, title="[bold]Active Worker Streams[/bold]", border_style="blue")

    # Logs Panel
    with log_lock:
        logs_content = Group(*log_messages) if log_messages else Text("[dim italic]No active logs[/dim italic]")
    logs_panel = Panel(logs_content, title="[bold]System Log & Diagnostics[/bold]", border_style="yellow", height=8)

    # Global Progress Bar
    pct = (processed_count / total_count * 100) if total_count > 0 else 0
    bar_width = 40
    filled = int(pct / 100 * bar_width)
    empty = bar_width - filled
    
    # Modern heavy horizontal line progress bar
    bar_str = "━" * filled + ("╸" if empty > 0 else "") + " " * (empty - 1 if empty > 0 else 0)
    
    global_progress_text = Text.assemble(
        ("Global Progress: ", "bold white"),
        ("[", "dim white"),
        (f"{bar_str}", "bold green" if pct == 100 else "bold yellow"),
        ("] ", "dim white"),
        (f"{pct:.1f}%", "bold green" if pct == 100 else "bold yellow"),
        (f" ({processed_count}/{total_count} Files Complete)", "white")
    )
    global_panel = Panel(global_progress_text, border_style="green" if pct == 100 else "cyan", box=box.ROUNDED)

    layout = Layout()
    layout.split_column(
        Layout(header, size=5),
        Layout(jobs_panel, ratio=1),
        Layout(logs_panel, size=8),
        Layout(global_panel, size=3)
    )
    return layout


# --- Download Worker Task ---

def download_item(session: MoodleSession, url: str, dest_path: str, worker_id: int) -> str:
    """Streams file download and checks for redirects/embeds. Returns status."""
    try:
        response = session.get(url, stream=True, allow_redirects=True, timeout=20)
        response.raise_for_status()
    except Exception as e:
        safe_log(f"Connection failed: {url} -> {e}", "red")
        with global_lock:
            failed_downloads.append((url, f"Connection failed: {e}"))
        return "failed"

    final_url = response.url
    content_type = response.headers.get('Content-Type', '')

    # Handle embedded resource HTML wrapper redirects
    if 'text/html' in content_type:
        try:
            html_content = response.text
            soup = BeautifulSoup(html_content, 'html.parser')
            actual_url = None
            for tag in soup.find_all(['iframe', 'embed', 'object', 'a'], href=True) + soup.find_all(['iframe', 'embed', 'object'], src=True):
                target = tag.get('href') or tag.get('src') or tag.get('data')
                if target and 'pluginfile.php' in target:
                    actual_url = urllib.parse.urljoin(final_url, target)
                    break

            if not actual_url:
                match = re.search(r'(?:href|src|data)="([^"]*pluginfile\.php[^"]*)"', html_content)
                if not match:
                    match = re.search(r"(?:href|src|data)='([^'*pluginfile\.php[^']*)'", html_content)
                if match:
                    actual_url = urllib.parse.urljoin(final_url, match.group(1))

            if actual_url:
                response.close()
                return download_item(session, actual_url, dest_path, worker_id)
            else:
                safe_log(f"Could not locate actual file URL in HTML wrapper: {url}", "yellow")
                with global_lock:
                    failed_downloads.append((url, "Could not locate actual file URL in HTML wrapper"))
                return "failed"
        except Exception as e:
            safe_log(f"Failed parsing embed: {e}", "red")
            with global_lock:
                failed_downloads.append((url, f"Failed parsing HTML wrapper: {e}"))
            return "failed"

    # Parse standard file metadata
    filename = ""
    cd = response.headers.get('Content-Disposition')
    if cd:
        match = re.search(r'filename\*=UTF-8\'\'(.+)', cd)
        if match:
            filename = urllib.parse.unquote(match.group(1))
        else:
            match = re.search(r'filename="([^"]+)"', cd)
            if match:
                filename = match.group(1)
            else:
                match = re.search(r'filename=([^;]+)', cd)
                if match:
                    filename = match.group(1).strip()

    if not filename:
        parsed = urllib.parse.urlparse(final_url)
        filename = posixpath.basename(urllib.parse.unquote(parsed.path))
        if not filename or filename in ('view.php', 'pluginfile.php'):
            filename = 'unknown_file'

    filename = clean_filename(filename)
    full_dest = os.path.join(dest_path, filename)

    total_size = response.headers.get('Content-Length')
    if total_size:
        total_size = int(total_size)
    else:
        total_size = None

    # Idempotency / Skip Match Check
    if os.path.exists(full_dest):
        local_size = os.path.getsize(full_dest)
        if total_size is not None and local_size == total_size:
            response.close()
            return "skipped"
        elif total_size is None:
            response.close()
            return "skipped"

    # Set worker job state
    with active_jobs_lock:
        active_jobs[worker_id] = {
            "filename": filename,
            "dest": os.path.basename(dest_path)
        }

    prog = worker_progresses[worker_id]
    for t in list(prog.tasks):
        prog.remove_task(t.id)
    task_id = prog.add_task(description="", total=total_size)

    safe_log(f"Downloading: {filename}", "cyan")

    # Save output stream
    os.makedirs(os.path.dirname(full_dest), exist_ok=True)
    try:
        with open(full_dest, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    prog.update(task_id, advance=len(chunk))

        safe_log(f"Saved: {filename}", "green")
        return "downloaded"
    except Exception as e:
        safe_log(f"Write failed: {filename} -> {e}", "red")
        with global_lock:
            failed_downloads.append((filename, f"Write failed: {e}"))
        return "failed"
    finally:
        response.close()
        with active_jobs_lock:
            active_jobs[worker_id] = None


def worker_task(task: dict, worker_id: int, session: MoodleSession):
    """Executes single task wrapper with thread-safe counters."""
    global completed_files, skipped_files, failed_files, processed_files
    url = task['url']
    dest = task['dest']
    status = download_item(session, url, dest, worker_id)

    with global_lock:
        processed_files += 1
        if status == "downloaded":
            completed_files += 1
        elif status == "skipped":
            skipped_files += 1
        else:
            failed_files += 1


# --- Interactive CLI Helper ---

def select_course_sections(sections: list) -> list:
    """Displays terminal checkbox menu for user section selection."""
    if len(sections) == 1:
        return sections

    choices = [
        {"name": f"{sec['name']} ({len(sec['items'])} items)", "value": sec}
        for sec in sections
    ]
    for choice in choices:
        choice["enabled"] = True

    try:
        result = inquirer.checkbox(
            message="Select course sections to download:",
            choices=choices,
            instruction="(Space to toggle, Enter to confirm, Ctrl+C to abort)",
            vi_mode=False
        ).execute()

        if not result:
            console.print("[-] No sections selected. Exiting.", style="red")
            sys.exit(0)
        return result
    except KeyboardInterrupt:
        console.print("\n[!] Selection cancelled. Exiting.", style="yellow")
        sys.exit(0)


# --- Core Executive main ---

def main():
    console.print(BANNER, style="bold cyan")

    parser = argparse.ArgumentParser(description="Download all contents of a Moodle course.")
    parser.add_argument("-u", "--url", help="Moodle course page URL or ID")
    parser.add_argument("-c", "--cookie", help="MoodleSession cookie value")
    parser.add_argument("-o", "--output", default="./moodle_downloads", help="Output directory to save downloads")
    parser.add_argument("-w", "--workers", type=int, help="Number of concurrent download workers")
    parser.add_argument("-i", "--interval", type=int, help="Check interval in minutes (if set, script runs periodically)")
    parser.add_argument("-y", "--all", action="store_true", help="Auto-select all sections (bypass selection menu)")
    parser.add_argument("--username", help="Moodle username")
    parser.add_argument("--password", help="Moodle password")

    args = parser.parse_args()

    course_url = args.url
    cookie_val = args.cookie
    username = args.username
    password = args.password
    output_dir = args.output
    workers = args.workers
    interval = args.interval
    auto_select_all = args.all

    if not course_url:
        course_url = input("Enter Moodle Course URL (or ID, e.g. 209): ").strip()

    if course_url.isdigit():
        course_url = f"https://moodle.ucsy.edu.mm/course/view.php?id={course_url}"
    elif 'id=' not in course_url:
        console.print("[-] Invalid Course URL. Must contain a course ID (?id=...)", style="red")
        return 1

    parsed_url = urllib.parse.urlparse(course_url)
    base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"

    session = MoodleSession(base_url)
    auth_success = False

    # 1. Try Cookie Caching Load
    cached = load_cached_session(course_url)
    if cached and not cookie_val and not (username and password):
        console.print("[*] Found cached session. Checking validity...", style="cyan")
        session.inject_cookies(cached["cookies"])
        try:
            res = session.get(course_url, allow_redirects=True, timeout=15)
            if "login/index.php" not in res.url:
                auth_success = True
                username = cached.get("username", "Cached User")
                console.print(f"[+] Re-used valid session cache for user: {username}", style="green")
            else:
                console.print("[-] Cached session expired.", style="yellow")
                clear_session_cache()
        except Exception:
            console.print("[-] Failed connecting with cached session.", style="yellow")
            clear_session_cache()

    # 2. Command Line or Interactive Auth Flow
    if not auth_success:
        if not cookie_val and not (username and password):
            while True:
                console.print("\nAuthentication Options:", style="bold yellow")
                console.print("1) Enter MoodleSession Cookie (Recommended - bypasses captcha/SSO)", style="green")
                console.print("2) Enter Username & Password", style="green")
                auth_choice = input("Select login method (1 or 2): ").strip()

                if auth_choice == '1':
                    cookie_val = input("MoodleSession Cookie: ").strip()
                    break
                elif auth_choice == '2':
                    username = input("Username: ").strip()
                    password = getpass.getpass("Password: ")
                    break
                else:
                    console.print("[!] Invalid option. Please select 1 or 2.", style="bold red")

        if cookie_val:
            console.print("[*] Injecting session cookie...", style="cyan")
            session.set_cookie(cookie_val)
            auth_success = True
        elif username and password:
            console.print(f"[*] Authenticating as '{username}'...", style="cyan")
            success, msg = session.login(username, password)
            if success:
                console.print(f"[+] {msg}", style="green")
                auth_success = True
                # Cache successful session cookies
                save_session_cache(course_url, session.session.cookies.get_dict(), username)
            else:
                console.print(f"[-] {msg}", style="red")
                return 1

    if not auth_success:
        console.print("[-] Authentication failed. Exiting.", style="red")
        return 1

    # 3. Choose running interval
    if interval is None:
        ans = input("Would you like this script to run periodically to check for updates? (y/n, default: n): ").strip().lower()
        if ans == 'y':
            try:
                val = input("Enter check interval in minutes (default: 10): ").strip()
                interval = int(val) if val else 10
            except ValueError:
                interval = 10
        else:
            interval = 0

    # 4. Initialize concurrency workers
    if workers is None:
        ans = input(f"Would you like to download files in parallel? (y/n, default: y): ").strip().lower()
        if ans == 'n':
            workers = 1
        else:
            workers = 4

    # Initialize shared states
    global active_jobs, worker_progresses
    active_jobs = [None] * workers
    worker_progresses = []
    for _ in range(workers):
        progress = Progress(
            TextColumn("[bold green]{task.percentage:>3.0f}%"),
            BarColumn(bar_width=12, style="bright_black", complete_style="bold green", finished_style="bold green"),
            StyledDownloadColumn(),
            StyledTransferSpeedColumn(),
            StyledTimeRemainingColumn(),
            console=console
        )
        worker_progresses.append(progress)

    run_count = 0
    try:
        while True:
            run_count += 1
            console.print(f"[*] Fetching course content structure...", style="cyan")
            try:
                res = session.get(course_url, allow_redirects=True, timeout=20)
                if "login/index.php" in res.url:
                    console.print("[-] Session expired. Attempting re-authentication...", style="yellow")
                    if username and password:
                        success, _ = session.login(username, password)
                        if success:
                            res = session.get(course_url, timeout=20)
                        else:
                            console.print("[-] Re-authentication failed. Exiting.", style="red")
                            return 1
                    else:
                        console.print("[-] Re-authentication unavailable without credentials. Exiting.", style="red")
                        return 1
                html_content = res.text
            except Exception as e:
                console.print(f"[-] Network connection error: {e}", style="red")
                if interval == 0:
                    return 1
                time.sleep(30)
                continue

            # Parse Course Content sections
            sections = parse_course_page(html_content)
            if not sections:
                console.print("[-] Course metadata parsing failed or course has no sections.", style="red")
                if interval == 0:
                    return 1
                time.sleep(30)
                continue

            # Extract course title and module code to organize folders
            course_title, module_code = extract_course_info(html_content)
            if module_code:
                course_dest_dir = os.path.join(output_dir, module_code, course_title)
            else:
                course_dest_dir = os.path.join(output_dir, course_title)

            # Select target sections: Auto-select if non-interactive interval or yes flag is set
            if run_count == 1 and not auto_select_all and interval == 0:
                selected_sections = select_course_sections(sections)
            else:
                selected_sections = sections

            # Build queue items flat list
            console.print("[*] Scanning sections and folders to create download queue...", style="cyan")
            download_tasks = []

            for section in selected_sections:
                sec_name = clean_filename(section['name'])
                sec_dir = os.path.join(course_dest_dir, sec_name)

                for item in section['items']:
                    if item['type'] == 'resource':
                        download_tasks.append({
                            'url': item['url'],
                            'dest': sec_dir
                        })
                    elif item['type'] == 'folder':
                        try:
                            f_res = session.get(item['url'], timeout=15)
                            folder_files = parse_folder_page(f_res.text)
                            if folder_files:
                                folder_name = clean_filename(item['name'])
                                folder_dir = os.path.join(sec_dir, folder_name)
                                for f_item in folder_files:
                                    download_tasks.append({
                                        'url': f_item['url'],
                                        'dest': folder_dir
                                    })
                        except Exception as e:
                            console.print(f"  [!] Skipping folder '{item['name']}': {e}", style="yellow")

            total_tasks = len(download_tasks)
            if total_tasks == 0:
                console.print("[+] No resources or files available to download.", style="green")
            else:
                # Reset counts
                global completed_files, skipped_files, failed_files, processed_files, failed_downloads
                with global_lock:
                    completed_files = 0
                    skipped_files = 0
                    failed_files = 0
                    processed_files = 0
                    failed_downloads = []

                with log_lock:
                    log_messages.clear()

                start_time = time.time()
                status_label = f"Check #{run_count} (Active)"

                # Execution Live Layout Loop
                with Live(make_dashboard_layout(start_time, status_label, 0, total_tasks, workers), console=console, screen=True, refresh_per_second=5) as live:
                    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
                        futures = [executor.submit(worker_task, task, i % workers, session) for i, task in enumerate(download_tasks)]

                        while True:
                            with global_lock:
                                current_processed = processed_files

                            live.update(make_dashboard_layout(start_time, status_label, current_processed, total_tasks, workers))

                            if current_processed == total_tasks:
                                break
                            time.sleep(0.2)

                        # Propagate exceptions if any occurred in background threads
                        for future in futures:
                            future.result()

                # Summary View Panel
                summary_table = Table.grid(padding=(0, 1))
                summary_table.add_column(style="bold yellow")
                summary_table.add_column(style="white")
                summary_table.add_row("Downloaded (New):", f"[bold green]{completed_files}[/bold green]")
                summary_table.add_row("Skipped (Unchanged):", f"[dim]{skipped_files}[/dim]")
                summary_table.add_row("Failed (Errors):", f"[bold red]{failed_files}[/bold red]")
                summary_table.add_row("Destination:", f"[cyan]{os.path.abspath(course_dest_dir)}[/cyan]")

                console.print()
                console.print(
                    Panel(
                        summary_table,
                        title=f"[bold green]Run #{run_count} Check Complete![/bold green]",
                        border_style="green",
                        box=box.ROUNDED,
                        expand=False
                    )
                )

                # Print failed downloads detail if any
                with global_lock:
                    fails = list(failed_downloads)
                if fails:
                    fail_table = Table(box=box.ROUNDED, border_style="red", expand=False)
                    fail_table.add_column("Resource / File / URL", style="bold red")
                    fail_table.add_column("Reason for Failure", style="white")
                    for item_name, reason in fails:
                        fail_table.add_row(item_name, reason)
                    
                    console.print()
                    console.print(
                        Panel(
                            fail_table,
                            title="[bold red]Failed Downloads Detail[/bold red]",
                            border_style="red",
                            expand=False
                        )
                    )

            if interval == 0:
                break

            # Countdown Timer inside Live block for periodic checks
            next_epoch = time.time() + interval * 60
            next_time_str = time.strftime('%H:%M:%S', time.localtime(next_epoch))

            with Live(Panel(Text(""), border_style="cyan"), console=console, refresh_per_second=1) as live:
                while time.time() < next_epoch:
                    remaining = int(next_epoch - time.time())
                    mins, secs = divmod(remaining, 60)
                    countdown_text = Text.assemble(
                        ("Waiting for the next update check...\n\n", "white"),
                        ("Next check scheduled at: ", "yellow"), (f"{next_time_str}\n", "white"),
                        ("Countdown: ", "yellow"), (f"{mins:02d}:{secs:02d}\n\n", "cyan bold"),
                        ("Press Ctrl+C to stop the script.", "dim italic")
                    )
                    live.update(Panel(countdown_text, title="UCSY Moodle Monitor", border_style="cyan", box=box.ROUNDED))
                    time.sleep(1)

    except KeyboardInterrupt:
        console.print("\n[!] Execution stopped by user. Goodbye!", style="bold yellow")
        return 0
    return 0

if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        console.print("\n[!] Interrupted by user. Exiting.", style="bold yellow")
        sys.exit(1)
