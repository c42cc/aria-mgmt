#!/usr/bin/env bash
# Interactively trigger every macOS TCC permission prompt that the bot needs.
#
# macOS only shows a permission dialog the FIRST time a process touches a
# protected resource. Background launchd processes can't surface these
# dialogs at all -- they just get EPERM. So we run each probe from this
# script (interactive shell) to force the prompts to appear, then point
# you at the right Settings panel to verify.
#
# After running this once and clicking Allow on each dialog, the macOS
# TCC database remembers the grant for the bot's parent process tree.

set -uo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "This script only runs on macOS." >&2
    exit 1
fi

PKG_ROOT="/opt/homebrew/lib/node_modules/mcp-macos"
EVENTKIT_CLI="$PKG_ROOT/bin/EventKitCLI"

banner() {
    echo ""
    echo "========================================================================"
    echo "$1"
    echo "========================================================================"
}

pause() {
    read -rp "Press Enter when you've responded to the dialog (or Cmd-Tab to find it)..." _
}

settings_panel() {
    local domain="$1"
    echo "  Settings panel: System Settings > Privacy & Security > $domain"
}

probe_eventkit_calendar() {
    banner "1. CALENDAR (EventKit)"
    if [[ ! -x "$EVENTKIT_CLI" ]]; then
        echo "WARN: EventKitCLI not built. Run: bash ops/build_macos_swift.sh"
        return
    fi
    echo "Triggering EventKit calendar access..."
    "$EVENTKIT_CLI" --action read-calendars 2>&1 | head -20 || true
    settings_panel "Calendars"
    pause
}

probe_eventkit_reminders() {
    banner "2. REMINDERS (EventKit)"
    if [[ ! -x "$EVENTKIT_CLI" ]]; then
        echo "WARN: EventKitCLI not built. Run: bash ops/build_macos_swift.sh"
        return
    fi
    echo "Triggering EventKit reminders access..."
    "$EVENTKIT_CLI" --action read-lists 2>&1 | head -20 || true
    settings_panel "Reminders"
    pause
}

probe_contacts() {
    banner "3. CONTACTS (AddressBook)"
    echo "Triggering Contacts access..."
    osascript -e 'tell application "Contacts" to count people' 2>&1 || true
    settings_panel "Contacts"
    pause
}

probe_mail() {
    banner "4. MAIL"
    echo "Triggering Mail access via AppleScript..."
    osascript -e 'tell application "Mail" to get name of every account' 2>&1 || true
    settings_panel "Automation > Terminal > Mail"
    pause
}

probe_notes() {
    banner "5. NOTES"
    echo "Triggering Notes access via AppleScript..."
    osascript -e 'tell application "Notes" to count notes' 2>&1 || true
    settings_panel "Automation > Terminal > Notes"
    pause
}

probe_messages() {
    banner "6. MESSAGES"
    echo "Triggering Messages access via AppleScript..."
    osascript -e 'tell application "Messages" to count chats' 2>&1 || true
    settings_panel "Automation > Terminal > Messages (and Full Disk Access for chat.db)"
    pause
}

probe_full_disk_access() {
    banner "7. FULL DISK ACCESS"
    echo "Full Disk Access cannot be triggered automatically — you must add the bot's"
    echo "Python interpreter manually. This grants the bot read access to ~/Library,"
    echo "Mail attachments, Messages history, etc."
    echo ""
    echo "Steps:"
    echo "  1. Open: System Settings > Privacy & Security > Full Disk Access"
    echo "  2. Click +"
    echo "  3. Press Cmd-Shift-G and paste:"
    echo "     /Users/corbin/PycharmProjects/agi_env_v1/ucs2/.venv/bin/python3.12"
    echo "  4. Toggle it ON."
    pause
}

main() {
    echo ""
    echo "macOS Permissions Bootstrap for UCS"
    echo ""
    echo "I'll exercise each macOS-protected capability the bot uses. Each one"
    echo "will pop a permission dialog. Click Allow on each."
    echo ""
    echo "If a dialog doesn't appear, the permission was probably already granted"
    echo "(or already denied -- in which case open System Settings and flip it on)."
    echo ""
    read -rp "Continue? [y/N] " ans
    [[ "$ans" =~ ^[Yy]$ ]] || exit 0

    probe_eventkit_calendar
    probe_eventkit_reminders
    probe_contacts
    probe_mail
    probe_notes
    probe_messages
    probe_full_disk_access

    banner "Done"
    echo "Verify: open System Settings > Privacy & Security and check that"
    echo "Calendars / Reminders / Contacts / Mail / Notes / Automation entries"
    echo "are all toggled ON for Terminal (or your shell) and the Python interpreter."
    echo ""
    echo "Then run: make preflight"
}

main
