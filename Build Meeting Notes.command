#!/bin/bash
# Double-click this in Finder to build the app and put it in your Applications
# folder. A Terminal window opens to show progress; you do not have to type
# anything. Close it when it says the app is installed.
cd "$(dirname "$0")" || exit 1
./build.sh --install
echo
echo "Done. Open it from Launchpad, or from your Applications folder."
echo "The icon appears in the menu bar at the top of the screen."
echo
read -r -p "Press Enter to close this window…"
