# Kali and training target images

`images/kali-desktop/` contains the original Astra #3 XFCE, VNC/noVNC, package lists, scripts and browser client. Its previously missing COPY inputs have been restored.

```bash
bash images/build.sh
```

This builds `cyberpod/kali-desktop:dev` and `cyberpod/hydra-target:dev`. A working Docker daemon and image/package network access are required. The script exits 77 when Docker is missing.

The target requires a runtime-provisioned JSON file at `/run/secrets/target.json` with `session_id`, `generation`, `username`, `password`, `flag`, and future Unix `expires_at`. The desktop requires the VNC configuration described in `kali-desktop/ASTRA3_HANDOFF.md`. Building the images alone does not provide the runtime/secret/gateway wiring.

`images/live/docker-compose.yml` is a **single-user connectivity fixture**, not a Kali workstation or multi-student platform. Its image tags are `cyberpod/desktop-probe:dev` and `cyberpod/target-probe:dev`, preventing it from overwriting the real image tags. Published probe ports bind only to loopback. The fixture uses a public training password and flag.

```bash
docker compose -f images/live/docker-compose.yml up --build
```

The fixture target now uses the same validated, expiry-aware handler as the actual target image.
