# Build Your Own OS Image

You can pre-build an image with all the engines you need. This can be faster
than uploading an engine each time/creating an engine when configuring a node.

The [packer](https://www.hashicorp.com/en/products/packer) utility is used
to build images. It must be installed beforehand.
See [documentation](https://developer.hashicorp.com/packer/install).

The details of creation will vary for different targets,
but the essence will be common.

The main configuration file for `packer` is the `*.pkr.hcl` file.
It specifies which plugin to use in the `packer` section,
what to use as OS image source in the `source` section and
how to build it in the `build` section.
The contents of this file will be different for different OS and clouds.
The common parts are in the scripts listed in the `source` section.

## FLEUR Engine on Debian 12 at Hetzner Cloud

In Hetzner Cloud, the OS image is called a "snapshot".
The snapshot can be used as a base for creating a "server".

You need to create a project in HCloud and create a read and write API key
in it. Then specify this API key in the `HCLOUD_TOKEN` environment variable.
For example, `export HCLOUD_TOKEN=“xxxx”`.

The main configuration file for the build is `hcloud-debian-12-fleur.pkr.hcl`.
Most likely you need to change the `location` and `server_type` in the
`source` section to match your HCloud project's settings.

There is a list of provision shell scripts in the `build` section.
The engine build script is located in the `install-fleur.sh` file.
The rest of the scripts perform common operations to prepare an OS image
and will not be significantly different for other OS/clouds/engines.

Let's build an image. To do this, install required `packer` plugins:

```sh
packer init ./hcloud-debian-12-fleur.pkr.hcl
```

Then run build script:

```sh
packer build ./hcloud-debian-12-fleur.pkr.hcl
```

This can take 15 to 20 minutes.
If everything is successful, the last line will show the image ID. Remember this ID - you will need to specify it in `yascheduler.conf`.

```
--> hcloud.debian: A snapshot was created: 'fleur-debian-xxxxx' (ID: 123123123)
```

As a result, we have an OS image (snapshot) with the usual
Debian 12 with `inpgen` and `fleur` already installed.

Let's configure `yascheduler` to use our OS image.
Setup `db`, `local` and `remote` sections in `yascheduler.conf` as usual. Add `clouds` and engines sections:

```ini
[engine.inpgen]
spawn = inpgen -explicit -inc +all -f aiida.in > shell.out 2> out.error
check_cmd = ps -eocomm= | grep -q inpgen
input_files = aiida.in
output_files = aiida.in inp.xml default.econfig shell.out out out.error scratch struct.xsf

[engine.fleur]
spawn = fleur -minimalOutput -wtime 360 > shell.out 2> out.error
check_cmd = ps -eocomm= | grep -q fleur
input_files = inp.xml
output_files = inp.xml kpts.xml sym.xml relax.xml shell.out out.error out out.xml FleurInputSchema.xsd FleurOutputSchema.xsd juDFT_times.json cdn1 usage.json

[clouds]
; Your API key
hetzner_token = xxx
; Your preffered server type.
; The disk should be at least as large as the snapshot.
hetzner_server_type = cpx21
; It's best to use the same location as the snapshot.
; This way servers will be created faster.
hetzner_location = fsn1
; OS image ID that you should have memorized earlier.
; You can always look under Snapshots in HCloud Dashboard.
hetzner_image_name = 237472643
```

With this configuration, `yascheduler` will build servers from our
OS image with pre-installed engines. This way, after spending time once,
we don't waste time building/loading the engine every time.
