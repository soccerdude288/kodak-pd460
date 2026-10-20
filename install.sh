#!/bin/sh
# Deploy the Kodak PD460 driver to a Linux CUPS host over SSH.
#
#   HOST=pi@printserver.local ./install.sh                     driver files only
#   HOST=pi@printserver.local ./install.sh AA:BB:CC:DD:EE:FF   also create a shared queue
#
# The host needs CUPS, python3-pil and Bluetooth; the remote user needs sudo.
set -eu

: "${HOST:?set HOST=user@host to the machine that will print}"
QUEUE=${QUEUE:-Kodak-PD460}
# CUPS rejects colons in the URI host part; the backend turns dashes back.
ADDR=$(echo "${1:-}" | tr : -)
cd "$(dirname "$0")"

scp -q kodak_dock.py cups/kodakdock cups/rastertokodakdock cups/kodak-pd460.ppd "$HOST:/tmp/"
ssh "$HOST" "sudo sh -eu" <<EOF
install -d -m 755 /usr/local/lib/kodak-dock /usr/share/ppd/kodak
install -m 644 /tmp/kodak_dock.py /usr/local/lib/kodak-dock/kodak_dock.py
install -m 755 /tmp/kodakdock /usr/lib/cups/backend/kodakdock
install -m 755 /tmp/rastertokodakdock /usr/lib/cups/filter/rastertokodakdock
install -m 644 /tmp/kodak-pd460.ppd /usr/share/ppd/kodak/kodak-pd460.ppd
rm -f /tmp/kodak_dock.py /tmp/kodakdock /tmp/rastertokodakdock /tmp/kodak-pd460.ppd
if [ -n "$ADDR" ]; then
  cupsctl --share-printers
  lpadmin -p "$QUEUE" -E -v "kodakdock://$ADDR" -P /usr/share/ppd/kodak/kodak-pd460.ppd \
    -D "Kodak PD460 (Bluetooth)" -L "\$(hostname)" -o printer-is-shared=true
  cupsenable "$QUEUE"; cupsaccept "$QUEUE"
fi
EOF
echo "installed on $HOST${ADDR:+, queue $QUEUE -> kodakdock://$ADDR}"
