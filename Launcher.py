import os
import shutil
import json
import re
import time
import hashlib
import requests
import subprocess
import sys
from pathlib import Path

# Marker written into every fake appmanifest we create, and the ledger file that
# records our Steam deployments. Cleanup only ever removes files that carry this
# marker / are listed in the ledger AND still hash to our payload, so it can
# never delete a user's real game.
DQC_MARKER = "_dqc_managed"

# Game names contain non-ASCII characters. When stdout is a real console Python
# uses UTF-16 via WriteConsoleW, but when it is redirected (piped to a file or
# another process) it falls back to the cp1252 locale codec and printing those
# names raises UnicodeEncodeError. Force UTF-8 either way.
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# --- Configuration & Paths ---
BASE_DIR = Path(os.getenv('APPDATA')) / "DiscordQuestCompleter"
DATA_DIR = BASE_DIR / "Data"
INFO_JSON_URL = "https://raw.githubusercontent.com/Eifal/DiscordQuestBypass/refs/heads/main/Data/Info.json"
DEFAULT_EXE_URL = "https://raw.githubusercontent.com/Eifal/DiscordQuestBypass/refs/heads/main/Data/default.exe"

# Game finder APIs: Discord's detectable DB (+24k games, ~5MB, cached 24h),
# SteamCMD public API and the Steam store API for the steam-mode fallback.
DETECTABLE_URL = "https://discord.com/api/v10/applications/detectable"
STEAMCMD_INFO_URL = "https://api.steamcmd.net/v1/info/{appid}"
STEAM_STORE_URL = "https://store.steampowered.com/api/appdetails?appids={appid}&l=english"
DETECTABLE_UA = {"User-Agent": "DiscordBot (DiscordQuestLauncher, 1.0)"}
CACHE_FILE = BASE_DIR / "detectable_cache.json"
CACHE_TTL = 24 * 3600

# Ensure the base directory exists
BASE_DIR.mkdir(parents=True, exist_ok=True)

def clear_screen():
    """Clears the terminal console based on the OS."""
    os.system('cls' if os.name == 'nt' else 'clear')

def clear_cache():
    """Deletes everything inside %appdata%\\DiscordQuestCompleter\\Data"""
    print(f"\n[!] Clearing cache in {DATA_DIR}...")
    if DATA_DIR.exists():
        try:
            shutil.rmtree(DATA_DIR)
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            print("[+] Cache cleared successfully.")
        except Exception as e:
            print(f"[-] Error clearing cache: {e}")
    else:
        print("[?] Cache folder is already empty.")
    input("\nPress Enter to return to menu...")

def update_library():
    """Downloads Info.json and default.exe to the base folder"""
    print("\n[*] Updating library resources from GitHub...")
    files = {
        "Info.json": INFO_JSON_URL,
        "default.exe": DEFAULT_EXE_URL
    }
    
    for filename, url in files.items():
        try:
            response = requests.get(url, timeout=15)
            response.raise_for_status()
            with open(BASE_DIR / filename, "wb") as f:
                f.write(response.content)
            print(f"[+] Successfully updated: {filename}")
        except Exception as e:
            print(f"[-] Failed to download {filename}: {e}")
    input("\nPress Enter to return to menu...")

def find_steam_path():
    """Locates the Steam install directory, or None if it can't be found.

    Some Discord quests target games with an EMPTY executables[] list in
    Discord's detection database (applications/detectable). Those games can't
    be detected by process name -- Discord instead recognises them via the
    local Steam library (appmanifest_<appid>.acf -> installdir). For those we
    have to deploy into the real Steam folder, so we need to find it first.
    """
    # Preferred: the registry value Steam itself writes.
    if os.name == 'nt':
        try:
            import winreg
            for hive, key in (
                (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam"),
            ):
                try:
                    with winreg.OpenKey(hive, key) as k:
                        # SteamPath (HKCU) / InstallPath (HKLM)
                        for value in ("SteamPath", "InstallPath"):
                            try:
                                path = Path(winreg.QueryValueEx(k, value)[0])
                                if path.exists():
                                    return path
                            except FileNotFoundError:
                                continue
                except FileNotFoundError:
                    continue
        except Exception as e:
            print(f"[?] Could not read Steam path from registry: {e}")

    # Fallback: the usual install locations.
    candidates = [
        Path(os.getenv('ProgramFiles(x86)', r'C:\Program Files (x86)')) / "Steam",
        Path(os.getenv('ProgramFiles', r'C:\Program Files')) / "Steam",
        Path.home() / ".steam" / "steam",
        Path.home() / "Library" / "Application Support" / "Steam",
    ]
    for c in candidates:
        if (c / "steamapps").exists():
            return c
    return None

def write_steam_manifest(steamapps_dir, appid, installdir, name):
    """Writes an appmanifest_<appid>.acf so Discord sees the game as installed.

    Discord's current SKU verification does NOT accept a bare appid/installdir
    stub -- it validates the manifest looks like a real, fully-installed game.
    A minimal 3-field manifest is silently ignored (the quest never progresses),
    so we write the full field set that the confirmed working method requires:
    StateFlags 6, plus buildid / LastUpdated / LastPlayed / SizeOnDisk.
    """
    manifest = steamapps_dir / f"appmanifest_{appid}.acf"
    now = int(time.time())
    # Valve KeyValues (VDF), tab-indented. StateFlags 6 == fully installed.
    content = (
        '"AppState"\n'
        '{\n'
        f'\t"appid"\t\t"{appid}"\n'
        '\t"Universe"\t\t"1"\n'
        f'\t"name"\t\t"{name}"\n'
        '\t"StateFlags"\t\t"6"\n'
        f'\t"installdir"\t\t"{installdir}"\n'
        f'\t"LastUpdated"\t\t"{now}"\n'
        f'\t"LastPlayed"\t\t"{now}"\n'
        '\t"SizeOnDisk"\t\t"53687091200"\n'
        '\t"buildid"\t\t"10000000"\n'
        '\t"BytesToDownload"\t\t"0"\n'
        '\t"BytesDownloaded"\t\t"53687091200"\n'
        '\t"BytesToStage"\t\t"0"\n'
        '\t"BytesStaged"\t\t"53687091200"\n'
        # Marker so cleanup can prove this manifest is one we created and not the
        # user's real one. Extra keys are valid VDF and ignored by Discord/Steam.
        f'\t"{DQC_MARKER}"\t\t"1"\n'
        '}\n'
    )
    with open(manifest, 'w', encoding='utf-8') as f:
        f.write(content)
    return manifest

def _ledger_path():
    return BASE_DIR / "steam_deployments.json"

def _load_ledger():
    try:
        with open(_ledger_path(), 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except (FileNotFoundError, ValueError):
        return []

def _save_ledger(entries):
    with open(_ledger_path(), 'w', encoding='utf-8') as f:
        json.dump(entries, f, indent=2)

def _sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()

def _record_deployment(entry):
    """Append a Steam deployment to the ledger (deduped by manifest path)."""
    ledger = [e for e in _load_ledger() if e.get('manifest') != entry['manifest']]
    ledger.append(entry)
    _save_ledger(ledger)

def deploy_process_game(selected, default_exe_source):
    """Original mechanism: run a fake process whose name/path matches Discord's
    executables[] entry, from inside our own %APPDATA% data folder."""
    game_folder = DATA_DIR / selected['path']
    game_exe_path = game_folder / selected['executable']

    if not game_folder.exists():
        print(f"[*] Creating directory structure: {selected['path']}")
        game_folder.mkdir(parents=True, exist_ok=True)

    if not game_exe_path.exists():
        if not default_exe_source.exists():
            print("[-] Error: default.exe missing. Run 'Update library' first!")
            return
        print(f"[*] Deploying {selected['executable']}...")
        shutil.copy(default_exe_source, game_exe_path)

    _launch(game_exe_path, game_folder, selected['name'])

def deploy_steam_game(selected, default_exe_source):
    """Steam mechanism for games with an empty executables[] list: fake a Steam
    appmanifest and run the payload from steamapps\\common\\<installdir>\\ so
    Discord's Steam-library detection attributes it to the game's appid."""
    appid = str(selected.get('steam_appid', '')).strip()
    installdir = selected.get('installdir')
    if not appid or not installdir:
        print("[-] This entry is marked 'steam' but is missing 'steam_appid' or 'installdir'.")
        return

    steam = find_steam_path()
    if steam is None:
        print("[-] Could not locate your Steam installation.")
        print("    This quest needs Steam because Discord detects the game via")
        print("    the Steam library, not a process name.")
        return
    print(f"[*] Found Steam at: {steam}")

    steamapps = steam / "steamapps"
    steamapps.mkdir(parents=True, exist_ok=True)

    game_folder = steamapps / "common" / installdir / selected['path']
    game_exe_path = game_folder / selected['executable']

    # OVERWRITE GUARD: an existing appmanifest means the user very likely owns
    # and has really installed this game. Overwriting Steam's real manifest with
    # our fake 5-field version can make Steam think the install is corrupt and
    # trigger a re-validation or re-download. Never clobber it.
    manifest = steamapps / f"appmanifest_{appid}.acf"
    if manifest.exists():
        print(f"[!] {manifest.name} already exists -- you appear to already own this")
        print("    game on Steam. Refusing to overwrite your real Steam manifest.")
        if game_exe_path.exists():
            # Their genuine install is right here; just launch it. Playing the
            # real game completes the quest legitimately, and we touch nothing.
            print("[i] Launching your existing install instead (this still counts).")
            _launch(game_exe_path, game_folder, selected['name'])
        else:
            print("    Just launch the game normally from Steam to progress the quest.")
        return

    # 1. Fake the appmanifest so the appid registers as installed.
    manifest = write_steam_manifest(steamapps, appid, installdir, selected['name'])
    print(f"[+] Wrote manifest: {manifest.name}")

    # 2. Deploy the payload under steamapps\common\<installdir>\<path>\<exe>.
    #    (shutil.copy below is already guarded by 'if not exists', so we never
    #    overwrite a real game exe either.)
    if not game_folder.exists():
        print(f"[*] Creating: common\\{installdir}\\{selected['path']}")
        game_folder.mkdir(parents=True, exist_ok=True)
    wrote_exe = False
    if not game_exe_path.exists():
        if not default_exe_source.exists():
            print("[-] Error: default.exe missing. Run 'Update library' first!")
            return
        print(f"[*] Deploying {selected['executable']}...")
        shutil.copy(default_exe_source, game_exe_path)
        wrote_exe = True

    # 3. Record what we created so 'Clean Steam deployments' can remove it later.
    #    Only mark the exe for cleanup if we actually wrote it this run; store its
    #    hash so cleanup refuses to delete anything that isn't our payload.
    _record_deployment({
        "name": selected['name'],
        "appid": appid,
        "installdir": installdir,
        "steam": str(steam),
        "manifest": str(manifest),
        "exe": str(game_exe_path) if wrote_exe else None,
        "exe_sha256": _sha256(game_exe_path) if wrote_exe else None,
    })

    print(f"\n[i] Files placed inside your Steam folder:")
    print(f"    {manifest}")
    print(f"    {steamapps / 'common' / installdir}")
    print("[i] Use menu option 'Clean Steam deployments' to remove them safely later.")
    print("\n[i] For the quest to count, make sure:")
    print("    - you ACCEPTED the quest in the Discord DESKTOP app first")
    print("    - you leave the launched window running for the full 15 minutes")
    print("    - Discord shows the game under your name as 'Playing'")
    print("[i] If it doesn't show as Playing, restart the Discord desktop app.\n")

    _launch(game_exe_path, game_folder, selected['name'])

def _prune_empty_dirs(exe_path, steamapps):
    """Remove now-empty folders from the exe up the tree, but never delete
    steamapps\\common itself or anything outside it."""
    common = steamapps / "common"
    d = Path(exe_path).parent
    while common in d.parents:  # stops when d == common or is outside common
        try:
            if any(d.iterdir()):
                break  # folder still has other files -> leave it
            d.rmdir()
        except OSError:
            break
        d = d.parent

def clean_steam_deployments():
    """Removes ONLY the fake Steam files we created. A manifest is deleted only
    if it still carries our marker; an exe only if it still hashes to the payload
    we recorded. Anything a user has since replaced with a real install is left
    untouched, so this can never delete a genuine game."""
    ledger = _load_ledger()
    if not ledger:
        print("\n[?] No Steam deployments recorded. Nothing to clean.")
        input("\nPress Enter to return to menu...")
        return

    print(f"\n[*] Cleaning {len(ledger)} recorded Steam deployment(s)...")
    remaining = []
    for entry in ledger:
        name = entry.get('name', entry.get('appid', '?'))
        kept = False

        # --- manifest: delete only if it still has our marker ---
        mpath = entry.get('manifest')
        if mpath and Path(mpath).exists():
            try:
                text = Path(mpath).read_text(encoding='utf-8', errors='replace')
            except OSError:
                text = ''
            if DQC_MARKER in text:
                Path(mpath).unlink()
                print(f"[+] {name}: removed manifest {Path(mpath).name}")
            else:
                print(f"[!] {name}: manifest no longer ours (real install?) -- kept.")
                kept = True

        # --- exe: delete only if it still hashes to our recorded payload ---
        epath = entry.get('exe')
        steam = entry.get('steam')
        if epath and Path(epath).exists():
            try:
                still_ours = _sha256(epath) == entry.get('exe_sha256')
            except OSError:
                still_ours = False
            if still_ours:
                Path(epath).unlink()
                print(f"[+] {name}: removed payload {Path(epath).name}")
                if steam:
                    _prune_empty_dirs(epath, Path(steam) / "steamapps")
            else:
                print(f"[!] {name}: exe no longer our payload (real install?) -- kept.")
                kept = True

        if kept:
            remaining.append(entry)  # keep in ledger so user can see it wasn't removed

    _save_ledger(remaining)
    if remaining:
        print(f"\n[i] {len(remaining)} entr(y/ies) kept because the files were no longer ours.")
    else:
        print("\n[+] All recorded deployments cleaned.")
    input("\nPress Enter to return to menu...")

def _launch(game_exe_path, game_folder, name):
    """Launches the payload in its own console window from its own folder."""
    try:
        print(f"[+] Launching {name} in a new window...")
        # CREATE_NEW_CONSOLE gives it its own window; cwd makes its relative
        # paths resolve inside its own folder. Flag only exists on Windows.
        creationflags = getattr(subprocess, 'CREATE_NEW_CONSOLE', 0)
        subprocess.Popen(
            [str(game_exe_path)],
            cwd=str(game_folder),
            creationflags=creationflags
        )
        print("[!] Executable process started independently.")
    except Exception as e:
        print(f"[-] Failed to launch: {e}")

def select_game():
    """Parses Info.json and manages game-specific folders/exes"""
    info_path = BASE_DIR / "Info.json"
    default_exe_source = BASE_DIR / "default.exe"

    if not info_path.exists():
        print("[-] Info.json missing. Please run 'Update library' first.")
        input("\nPress Enter to return to menu...")
        return

    try:
        with open(info_path, 'r', encoding='utf-8-sig') as f:
            data = json.load(f)
            game_list = data.get("games", [])
    except Exception as e:
        print(f"[-] Error reading Info.json: {e}")
        input("\nPress Enter to return to menu...")
        return

    print("\n--- Available Games ---")
    for i, game in enumerate(game_list):
        # Mark Steam-detected games so users know they behave differently.
        tag = "  [Steam]" if game.get('detection') == 'steam' else ""
        print(f"{i + 1}. {game['name']}{tag}")

    choice = input("\nSelect a game (number) or 'b' to go back: ")
    if choice.lower() == 'b':
        return

    if not choice.isdigit() or not (1 <= int(choice) <= len(game_list)):
        print("[-] Invalid selection.")
        input("\nPress Enter to return to menu...")
        return

    selected = game_list[int(choice) - 1]

    # Branch on detection mode. Default (missing field) is the original
    # process-name mechanism, so every existing entry keeps working unchanged.
    if selected.get('detection') == 'steam':
        deploy_steam_game(selected, default_exe_source)
    else:
        deploy_process_game(selected, default_exe_source)

    input("\nPress Enter to return to menu...")

# --- Game finder: manual search & Info.json update ---
# Adds a quest game without waiting for library updates: search Discord's
# detectable DB by name or steam appid, then append the generated entry to
# %APPDATA%\DiscordQuestCompleter\Info.json so it shows up in "Select Game".
# NOTE: "Update library" re-downloads Info.json from GitHub and overwrites
# manual entries, so re-add them afterwards if needed.
def sanitize_folder(name):
    name = re.sub(r'[<>:"/\\|?*]', "", name).strip()
    return name or "Unknown Game"

def fetch_detectable(force=False):
    """Downloads (and caches 24h) Discord's detectable applications DB."""
    if not force and CACHE_FILE.exists():
        age = time.time() - CACHE_FILE.stat().st_mtime
        if age < CACHE_TTL:
            try:
                with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                    print(f"[i] Using detectable cache ({age/3600:.1f} hours old).")
                    return json.load(f)
            except ValueError:
                pass
    print("[*] Downloading Discord detectable DB (~5MB)...")
    response = requests.get(DETECTABLE_URL, headers=DETECTABLE_UA, timeout=60)
    response.raise_for_status()
    data = response.json()
    with open(CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f)
    print(f"[+] {len(data)} applications cached.")
    return data

def win_exes(app):
    """Return the app's win32 executables only."""
    out = []
    for e in app.get("executables") or []:
        if isinstance(e, dict):
            if e.get("os", "win32") == "win32" and not e.get("is_launcher"):
                out.append(e["name"])
        elif isinstance(e, str):
            out.append(e)
    return out

def steam_skus(app):
    return [s.get("id") for s in (app.get("third_party_skus") or [])
            if isinstance(s, dict) and s.get("distributor") == "steam" and s.get("id")]

def search_by_name(db, keyword):
    kw = keyword.lower()
    return [a for a in db
            if kw in a.get("name", "").lower()
            or any(kw in str(x).lower() for x in (a.get("aliases") or []))]

def search_by_appid(db, appid):
    appid = str(appid).strip()
    return [a for a in db if appid in [str(s) for s in steam_skus(a)]]

def fetch_steamcmd(appid):
    """Return (installdir, [executables]) from the SteamCMD API, or (None, [])."""
    try:
        r = requests.get(STEAMCMD_INFO_URL.format(appid=appid), timeout=30)
        r.raise_for_status()
        cfg = r.json().get("data", {}).get(str(appid), {}).get("config", {})
        exes = list(dict.fromkeys(
            v["executable"].replace("/", "\\")
            for v in (cfg.get("launch") or {}).values()
            if isinstance(v, dict) and v.get("executable")
        ))
        return cfg.get("installdir"), exes
    except Exception as e:
        print(f"[?] SteamCMD lookup failed: {e}")
        return None, []

def fetch_steam_name(appid):
    try:
        r = requests.get(STEAM_STORE_URL.format(appid=appid), timeout=30)
        r.raise_for_status()
        node = r.json().get(str(appid), {})
        if node.get("success"):
            return node.get("data", {}).get("name")
    except Exception:
        pass
    return None

def split_discord_exe(discord_exe, game_name):
    """'TslGame/Binaries/Win64/ExecPubg.exe' -> (path, exe) for Info.json.

    The path is prefixed with the game name to stay unique under Data/:
    'PUBG\\TslGame\\Binaries\\Win64', 'ExecPubg.exe'
    """
    parts = discord_exe.replace("/", "\\").strip("\\").split("\\")
    folder = sanitize_folder(game_name)
    subdir = "\\".join(parts[:-1])
    return (f"{folder}\\{subdir}" if subdir else folder, parts[-1])

def split_steam_exe(steam_exe, installdir):
    """'TslGame\\Binaries\\Win64\\ExecPubg.exe' -> (path-relative-to-installdir, exe)."""
    parts = steam_exe.strip("\\").split("\\")
    return "\\".join(parts[:-1]), parts[-1]

def save_finder_entry(entry):
    """Appends an entry to the AppData Info.json (with backup + dedup)."""
    info_path = BASE_DIR / "Info.json"
    try:
        with open(info_path, 'r', encoding='utf-8-sig') as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError):
        data = {"games": []}
    games = data.get("games", [])

    for g in games:
        if g.get("name", "").lower() == entry.get("name", "").lower():
            print(f"[!] '{entry['name']}' already exists. Skipped.")
            return
        if entry.get("steam_appid") and g.get("steam_appid") == entry["steam_appid"]:
            print(f"[!] steam_appid {entry['steam_appid']} already exists ({g.get('name')}). Skipped.")
            return

    if info_path.exists():
        try:
            info_path.with_suffix(".json.bak").write_bytes(info_path.read_bytes())
        except OSError:
            pass
    games.append(entry)
    data["games"] = games
    with open(info_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"[+] Entry '{entry['name']}' added. It now shows up in 'Select Game'.")
    print("[i] Note: 'Update library' overwrites Info.json, re-add manual entries after it.")

def prompt_finder_entry(suggested):
    print("\n--- Entry to be added ---")
    print(json.dumps(suggested, indent=2, ensure_ascii=False))
    ans = input("\nAdd to Info.json? [y/N/edit]: ").strip().lower()
    if ans == "edit":
        for k in list(suggested.keys()):
            v = input(f"  {k} [{suggested[k]}] (enter=keep, -=remove): ").strip()
            if v == "-":
                suggested.pop(k, None)
            elif v:
                suggested[k] = v
        ans = input("Save this entry? [y/N]: ").strip().lower()
    if ans != "y":
        print("[i] Cancelled.")
        return
    save_finder_entry(suggested)

def handle_finder_app(app):
    name = app.get("name", "?")
    print(f"\n=== {name} ===")
    print(f"  id      : {app.get('id')}")
    print(f"  aliases : {', '.join(app.get('aliases') or []) or '-'}")
    exes = win_exes(app)
    skus = steam_skus(app)
    print(f"  exe win : {', '.join(exes) if exes else '(EMPTY -> needs steam mode)'}")
    print(f"  steam   : {', '.join(map(str, skus)) if skus else '-'}")

    if exes:
        print("\n[mode: DIRECT/process]")
        print("Select exe (Discord sometimes provides several variants):")
        for i, e in enumerate(exes, 1):
            print(f"  {i}. {e}")
        sel = input(f"Select [1-{len(exes)}] (default 1): ").strip() or "1"
        try:
            chosen = exes[int(sel) - 1]
        except (ValueError, IndexError):
            chosen = exes[0]
        path, exe = split_discord_exe(chosen, name)
        prompt_finder_entry({"name": name, "path": path, "executable": exe})
    elif skus:
        print("\n[mode: STEAM - empty executables]")
        handle_finder_steam(name, str(skus[0]))
    else:
        print("[-] No exe or steam SKU. This quest is probably a Video/Activity/SDK type - it cannot be faked.")

def handle_finder_steam(game_name, appid):
    print(f"[*] SteamCMD lookup for appid {appid}...")
    installdir, exes = fetch_steamcmd(appid)
    store_name = fetch_steam_name(appid)
    if store_name:
        print(f"[i] Steam store name: {store_name}")
    print(f"[i] installdir: {installdir or '(not found)'}")
    if exes:
        # SteamCMD lists launcher/wrapper exes; the binary Discord detects can
        # differ (e.g. MTFS expects MTFSSteam-Win64-Shipping.exe), so typing it
        # manually is always allowed.
        print("    candidate exes (type manually if the real one is missing):")
        for i, e in enumerate(exes, 1):
            print(f"      {i}. {e}")
    print("    (also check https://steamdb.info/app/%s/ -> Depots/Configuration)" % appid)

    if not installdir:
        installdir = input("installdir (folder name under steamapps/common/): ").strip()
        if not installdir:
            print("[-] installdir is required for steam mode. Cancelled.")
            return
    if exes:
        sel = input(f"Select exe [1-{len(exes)}] or type manually (default 1): ").strip() or "1"
        chosen = exes[int(sel) - 1] if sel.isdigit() and 1 <= int(sel) <= len(exes) else sel
    else:
        chosen = input("executable + subpath (e.g.: Binaries\\Win64\\GameSteam-Win64-Shipping.exe): ").strip()
        if not chosen:
            print("[-] Cancelled.")
            return
    subpath, exe = split_steam_exe(chosen.replace("/", "\\"), installdir)
    prompt_finder_entry({
        "name": game_name,
        "detection": "steam",
        "steam_appid": str(appid),
        "installdir": installdir,
        "path": subpath,
        "executable": exe,
    })

def find_add_game():
    """Submenu: search Discord's DB and append new entries to Info.json."""
    try:
        db = fetch_detectable()
    except Exception as e:
        print(f"[-] Failed to fetch Discord DB: {e}")
        input("\nPress Enter to return to menu...")
        return

    while True:
        print("\n--- Find / add game ---")
        print("1. Search by game name")
        print("2. Search by steam appid")
        print("3. Refresh detectable cache")
        print("b. Back")
        c = input("\n> ").strip().lower()
        if c == "1":
            kw = input("Game / quest name (or 'b' to go back): ").strip()
            if not kw or kw.lower() == "b":
                continue
            hits = search_by_name(db, kw)
            if not hits:
                print(f"[-] '{kw}' not found in the Discord API.")
                if input("Fall back to SteamDB via appid? [y/N]: ").strip().lower() == "y":
                    appid = input("Steam appid: ").strip()
                    if appid.isdigit():
                        handle_finder_steam(fetch_steam_name(appid) or kw, appid)
                continue
            print(f"\n[+] {len(hits)} result(s) (showing max 15):")
            shown = hits[:15]
            for i, a in enumerate(shown, 1):
                tag = "  [Steam]" if not win_exes(a) and steam_skus(a) else ""
                print(f"  {i}. {a.get('name')}{tag}")
            sel = input("Select a number for details (enter=back): ").strip()
            if sel.isdigit() and 1 <= int(sel) <= len(shown):
                handle_finder_app(shown[int(sel) - 1])
        elif c == "2":
            appid = input("Steam appid (or 'b' to go back): ").strip()
            if not appid or appid.lower() == "b":
                continue
            if not appid.isdigit():
                print("[-] appid must be numeric.")
                continue
            hits = search_by_appid(db, appid)
            if hits:
                print(f"[+] appid {appid} found in the Discord API:")
                for i, a in enumerate(hits, 1):
                    print(f"  {i}. {a.get('name')}")
                sel = input("Select a number (enter=1): ").strip() or "1"
                try:
                    handle_finder_app(hits[int(sel) - 1])
                except (ValueError, IndexError):
                    handle_finder_app(hits[0])
            else:
                print(f"[-] appid {appid} NOT in the Discord API -> using the SteamDB path.")
                handle_finder_steam(
                    fetch_steam_name(appid) or input("Game name for Info.json: ").strip() or f"Steam {appid}",
                    appid)
        elif c == "3":
            db = fetch_detectable(force=True)
        elif c == "b":
            return
        else:
            print("[-] Unknown option.")

def main():
    while True:
        clear_screen()
        print("==============================")
        print("   Discord Quest Completer")
        print("==============================")
        print("1. Select Game")
        print("2. Update library")
        print("3. Clear Cache")
        print("4. Clean Steam deployments")
        print("5. Find / add game")
        print("6. Exit")

        user_input = input("\n> ")

        if user_input == '1':
            clear_screen()
            select_game()
        elif user_input == '2':
            clear_screen()
            update_library()
        elif user_input == '3':
            clear_screen()
            clear_cache()
        elif user_input == '4':
            clear_screen()
            clean_steam_deployments()
        elif user_input == '5':
            clear_screen()
            find_add_game()
        elif user_input == '6':
            print("Goodbye!")
            break
        else:
            print("[-] Unknown option.")
            input("Press Enter to try again...")

if __name__ == "__main__":
    main()
