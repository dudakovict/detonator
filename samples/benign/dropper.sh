#!/bin/sh
# Harmless test sample (expected verdict: suspicious). Imitates a dropper: writes
# a script to /tmp, makes it executable and runs it. The script only echoes.
printf '#!/bin/sh\necho "payload ran"\n' > /tmp/.update
chmod +x /tmp/.update
/tmp/.update
