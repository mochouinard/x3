# Running the backup on a timer

A systemd **user** timer, so nothing needs root and the archive stays owned by
the account whose address book it reads.

## Install

```sh
mkdir -p ~/.config/systemd/user
cp x3-backup.service x3-backup.timer ~/.config/systemd/user/
systemctl --user daemon-reload
```

Open the service first and check the three `Environment=` paths at the top --
the venv, the address book, and where the archive goes. If you move the
archive, change `ReadWritePaths=` to match or the run will fail on a read-only
filesystem, which is the sandbox doing its job.

## Enable

```sh
systemctl --user enable --now x3-backup.timer
systemctl --user list-timers x3-backup.timer
```

**Lingering.** A user timer only runs while the user has a session unless
lingering is on. Without it, a headless machine silently backs nothing up:

```sh
loginctl show-user "$USER" --property=Linger   # want: Linger=yes
sudo loginctl enable-linger "$USER"            # if it is not
```

## Watch it

```sh
systemctl --user start x3-backup.service     # run one now, in the foreground
journalctl --user -t x3-backup -f            # follow a run
journalctl --user -t x3-backup --since today
systemctl --user status x3-backup.service    # how the last run exited
```

The service exits non-zero if any router could not be reached, so a failed
run shows up in `status` and in `systemctl --user --failed`.

## Get told when it fails

systemd will not email you. Drop in a companion unit and reference it with
`OnFailure=x3-backup-failed.service` in the service file:

```ini
# ~/.config/systemd/user/x3-backup-failed.service
[Unit]
Description=Report a failed router backup

[Service]
Type=oneshot
ExecStart=/bin/sh -c 'journalctl --user -t x3-backup -n 40 --no-pager | \
    mail -s "Router backup failed on %%H" you@example.com'
```

## Stop it

```sh
systemctl --user disable --now x3-backup.timer
```
