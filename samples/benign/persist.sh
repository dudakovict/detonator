#!/bin/sh
# Harmless test sample (expected verdict: malicious). Imitates persistence: adds a
# crontab entry and a .bashrc line, both of which only run /bin/true.
(crontab -l 2>/dev/null; echo "@reboot /bin/true # detonator-test") | crontab -
echo "/bin/true # detonator-test" >> "$HOME/.bashrc"
