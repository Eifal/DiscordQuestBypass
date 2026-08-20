import os
import shutil
import json
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
INFO_JSON_URL = "https://raw.githubusercontent.com/vaaanir/DiscordQuestBypass/refs/heads/main/Data/Info.json"
DEFAULT_EXE_URL = "https://raw.githubusercontent.com/vaaanir/DiscordQuestBypass/refs/heads/main/Data/default.exe"

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
        print("5. Exit")

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
            print("Goodbye!")
            break
        else:
            print("[-] Unknown option.")
            input("Press Enter to try again...")

if __name__ == "__main__":
    main()
