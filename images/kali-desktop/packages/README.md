# Tool installation strategy

The explicit base package list is `base.txt`: XFCE components, Firefox ESR,
TigerVNC, noVNC/websockify, terminal, networking utilities, Nmap, Python and Git.
There is no full Kali metapackage and no package installation at session startup.

The default `mvp` profile adds only packages in `mvp.txt` (currently the Hydra
executable). Select `CYBERPOD_TOOL_PROFILE=base` to remove the MVP tool without
changing any Core/desktop code. Lab training data and validators stay in plugins.

CI may pass the preserved `CYBERPOD_EXTRA_PACKAGES` argument with approved package
names or exact `package=version` pins. The installer validates token syntax and
passes arguments without eval. This is build configuration, never student input.

For a future toolset, create a derived image owned by the relevant Lab/image
maintainer, install an explicit reviewed package list as root at build time,
then return to `USER student`. Do not add generic sudo/root or NET_ADMIN.
Nmap TCP connect scans work without raw socket capability; packet capture,
SYN scans and other special operations need a separately approved Lab profile
owned by Astra #2.

The build accepts a pinned `KALI_BASE`. A base-image digest alone does not pin the
mutable apt repository. Record `/opt/cyberpod/build/packages.tsv`, scan the built
image, test it, and deploy the resulting immutable image digest. Use an approved
apt snapshot/version policy if bit-for-bit rebuilds are required. Package
availability and actual image size must be verified by the worker build.
