#!/usr/bin/env bash

# ============================================================
# Defrost Speaker Manager
# ============================================================
#
# Interactive utility for managing which ALSA playback device
# Project Defrost uses.
#
# Features:
#   - List all ALSA playback devices
#   - Test a specific device
#   - Automatically test every available device one-by-one
#   - Save the working device behind a friendly ALSA alias
#   - Test the saved "defrost" alias
#   - Preserve unrelated ~/.asoundrc configuration
#
# Usage:
#   ./speaker-manager.sh
#
# No arguments are required.
# ============================================================

set -u

# ------------------------------------------------------------
# Configuration
# ------------------------------------------------------------

ASOUNDRC="$HOME/.asoundrc"

# Friendly ALSA device name that applications should use.
ALIAS_NAME="defrost"

# Markers allow us to safely replace ONLY our section of
# ~/.asoundrc without destroying anything else in that file.
START_MARKER="# --- defrost-speaker-manager START ---"
END_MARKER="# --- defrost-speaker-manager END ---"

# Length of automatic speaker tests in seconds.
TEST_SECONDS=3

# Tone frequency used by speaker-test.
TEST_FREQUENCY=440


# ------------------------------------------------------------
# Helper functions
# ------------------------------------------------------------

pause() {
    echo
    read -rp "Press Enter to continue..."
}


header() {
    clear
    echo "=========================================="
    echo "        Defrost Speaker Manager"
    echo "=========================================="
    echo
}


# Return all ALSA device names reported by `aplay -L`.
#
# aplay -L output looks like:
#
#   null
#       Discard all samples...
#   hw:CARD=Device,DEV=0
#       USB Audio...
#
# Device names have no leading whitespace.
# Description lines do.
get_devices() {
    aplay -L | grep -v '^[[:space:]]' | grep -v '^$'
}


list_devices() {
    echo "Available ALSA playback devices:"
    echo

    local i=1
    while IFS= read -r device; do
        printf "%2d) %s\n" "$i" "$device"
        ((i++))
    done < <(get_devices)

    echo
}


# ------------------------------------------------------------
# Test one manually-entered ALSA device
# ------------------------------------------------------------

test_device() {
    list_devices

    read -rp "Enter ALSA device name to test: " DEVICE

    if [[ -z "$DEVICE" ]]; then
        echo "No device entered."
        return
    fi

    echo
    echo "Testing:"
    echo "  $DEVICE"
    echo
    echo "Press Ctrl+C to stop."
    echo

    speaker-test \
        -D "$DEVICE" \
        -t sine \
        -f "$TEST_FREQUENCY" \
        -c 2
}


# ------------------------------------------------------------
# Test every ALSA device
# ------------------------------------------------------------
#
# This is useful after changing USB speakers or sound cards.
#
# Each device gets a short test tone.
# Afterward you'll be asked:
#
#   Did that one make noise? (y/n)
#
# Answering YES stops the search and offers to save it.
#
# Answering NO moves to the next device.
# ------------------------------------------------------------

find_speaker() {

    mapfile -t DEVICES < <(get_devices)

    if [[ ${#DEVICES[@]} -eq 0 ]]; then
        echo "No ALSA playback devices found."
        return
    fi

    echo "Found ${#DEVICES[@]} ALSA playback devices."
    echo
    echo "Each device will play a ${TEST_SECONDS}-second test tone."
    echo "Some virtual devices may fail. That's normal."
    echo
    echo "Press Ctrl+C at any time to stop."
    echo

    read -rp "Start testing? [Y/n]: " START

    if [[ "$START" =~ ^[Nn]$ ]]; then
        return
    fi

    echo

    local number=1

    for DEVICE in "${DEVICES[@]}"; do

        echo "------------------------------------------"
        echo "Device $number of ${#DEVICES[@]}"
        echo
        echo "$DEVICE"
        echo "------------------------------------------"
        echo

        # timeout prevents speaker-test from running forever.
        #
        # stderr is hidden because many ALSA virtual devices
        # naturally throw errors during this process.
        timeout "$TEST_SECONDS" \
            speaker-test \
            -D "$DEVICE" \
            -t sine \
            -f "$TEST_FREQUENCY" \
            -c 2 \
            >/dev/null 2>&1

        echo

        while true; do

            read -rp "Did that one make noise? [y/n/q]: " ANSWER

            case "$ANSWER" in

                y|Y|yes|YES)
                    echo
                    echo "Found working speaker:"
                    echo
                    echo "  $DEVICE"
                    echo

                    read -rp "Set this as '$ALIAS_NAME'? [Y/n]: " SAVE

                    if [[ ! "$SAVE" =~ ^[Nn]$ ]]; then
                        save_device "$DEVICE"
                    fi

                    return
                    ;;

                n|N|no|NO)
                    echo
                    break
                    ;;

                q|Q|quit|QUIT)
                    echo
                    echo "Speaker search cancelled."
                    return
                    ;;

                *)
                    echo "Enter y, n, or q."
                    ;;
            esac

        done

        ((number++))
    done

    echo
    echo "Finished testing every ALSA device."
    echo "No working speaker was selected."
}


# ------------------------------------------------------------
# Remove our managed ~/.asoundrc section
# ------------------------------------------------------------

remove_managed_block() {

    [[ -f "$ASOUNDRC" ]] || return

    # Remove everything between our START and END markers.
    #
    # Anything outside those markers stays untouched.
    awk \
        -v start="$START_MARKER" \
        -v end="$END_MARKER" '
            $0 == start { skip=1; next }
            $0 == end   { skip=0; next }
            !skip
        ' "$ASOUNDRC" > "${ASOUNDRC}.tmp"

    mv "${ASOUNDRC}.tmp" "$ASOUNDRC"
}


# ------------------------------------------------------------
# Save physical ALSA device behind friendly alias
# ------------------------------------------------------------

save_device() {

    local DEVICE="$1"

    # Create ~/.asoundrc if it does not exist.
    touch "$ASOUNDRC"

    # Always make a backup before modifying anything.
    cp "$ASOUNDRC" "${ASOUNDRC}.bak"

    # Remove our old configuration first.
    remove_managed_block

    # Append our new managed section.
    cat >> "$ASOUNDRC" <<EOF

$START_MARKER

# Friendly Project Defrost speaker alias.
#
# Applications can use:
#
#   defrost
#
# instead of knowing the physical ALSA device name.

pcm.$ALIAS_NAME {
    type plug

    slave {
        pcm "$DEVICE"
    }
}

$END_MARKER
EOF

    echo
    echo "Speaker configuration saved."
    echo
    echo "Friendly name:"
    echo "  $ALIAS_NAME"
    echo
    echo "Physical device:"
    echo "  $DEVICE"
    echo
    echo "Backup:"
    echo "  ${ASOUNDRC}.bak"
}


# ------------------------------------------------------------
# Manually select a device and save it
# ------------------------------------------------------------

select_device() {

    list_devices

    read -rp "Enter ALSA device name: " DEVICE

    if [[ -z "$DEVICE" ]]; then
        echo "No device entered."
        return
    fi

    echo
    echo "Testing '$DEVICE'..."
    echo

    timeout "$TEST_SECONDS" \
        speaker-test \
        -D "$DEVICE" \
        -t sine \
        -f "$TEST_FREQUENCY" \
        -c 2 \
        >/dev/null 2>&1

    echo

    read -rp "Did that one make noise? [y/N]: " CONFIRM

    if [[ "$CONFIRM" =~ ^[Yy]$ ]]; then
        save_device "$DEVICE"
    else
        echo "Configuration unchanged."
    fi
}


# ------------------------------------------------------------
# Display current managed configuration
# ------------------------------------------------------------

show_current() {

    echo "Current '$ALIAS_NAME' configuration:"
    echo

    if [[ ! -f "$ASOUNDRC" ]]; then
        echo "No ~/.asoundrc exists."
        return
    fi

    if ! grep -qF "$START_MARKER" "$ASOUNDRC"; then
        echo "No Defrost speaker is currently configured."
        return
    fi

    sed -n "/$START_MARKER/,/$END_MARKER/p" "$ASOUNDRC"
}


# ------------------------------------------------------------
# Test friendly Defrost alias
# ------------------------------------------------------------

test_alias() {

    echo "Testing:"
    echo "  $ALIAS_NAME"
    echo
    echo "Press Ctrl+C to stop."
    echo

    speaker-test \
        -D "$ALIAS_NAME" \
        -t sine \
        -f "$TEST_FREQUENCY" \
        -c 2
}


# ------------------------------------------------------------
# Remove Defrost ALSA alias
# ------------------------------------------------------------

remove_alias() {

    if [[ ! -f "$ASOUNDRC" ]] ||
       ! grep -qF "$START_MARKER" "$ASOUNDRC"; then

        echo "No managed '$ALIAS_NAME' alias exists."
        return
    fi

    cp "$ASOUNDRC" "${ASOUNDRC}.bak"

    remove_managed_block

    echo "Removed '$ALIAS_NAME' configuration."
    echo
    echo "Backup saved to:"
    echo "  ${ASOUNDRC}.bak"
}


# ------------------------------------------------------------
# Main interactive menu
# ------------------------------------------------------------

while true; do

    header

    echo "Manage the speaker used by Project Defrost."
    echo
    echo "  1) List playback devices"
    echo "  2) Test one playback device"
    echo "  3) Find speaker - test ALL devices"
    echo "  4) Select/change Defrost speaker"
    echo "  5) Show current Defrost speaker"
    echo "  6) Test Defrost speaker"
    echo "  7) Remove Defrost speaker alias"
    echo "  8) Exit"
    echo

    read -rp "Choose an option [1-8]: " CHOICE

    echo

    case "$CHOICE" in

        1)
            list_devices
            pause
            ;;

        2)
            test_device
            pause
            ;;

        3)
            find_speaker
            pause
            ;;

        4)
            select_device
            pause
            ;;

        5)
            show_current
            pause
            ;;

        6)
            test_alias
            pause
            ;;

        7)
            remove_alias
            pause
            ;;

        8)
            exit 0
            ;;

        *)
            echo "Invalid option."
            sleep 1
            ;;
    esac

done
