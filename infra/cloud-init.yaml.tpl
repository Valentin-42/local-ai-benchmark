#cloud-config
# Rendered by infra/launch.sh. Keep this minimal: real logic lives in the repo
# (infra/vm-entry.sh) so it can change without touching the template.
package_update: true
packages: [git, curl, python3-yaml]

write_files:
  - path: /etc/bench.env
    permissions: "0600"
    content: |
      REPO=__REPO__
      BRANCH=__BRANCH__
      MACHINE=__MACHINE__
      GITHUB_TOKEN=__GITHUB_TOKEN__
      HF_TOKEN=__HF_TOKEN__
      GRACE_MINUTES=__GRACE_MINUTES__
      DRY_RUN=__DRY_RUN__

  - path: /usr/local/bin/bench-selfdestruct
    permissions: "0755"
    content: |
      #!/bin/bash
      # Delete this VM's whole resource group (VM, disk, IP, NIC...) using its
      # managed identity, which has Contributor on that resource group only.
      IMDS=http://169.254.169.254/metadata
      q() { curl -sf -H Metadata:true "$IMDS/instance/compute/$1?api-version=2021-02-01&format=text"; }
      SUB=$(q subscriptionId); RG=$(q resourceGroupName)
      for i in $(seq 1 40); do
        TOKEN=$(curl -sf -H Metadata:true "$IMDS/identity/oauth2/token?api-version=2018-02-01&resource=https://management.azure.com/" \
                | python3 -c 'import sys,json; print(json.load(sys.stdin)["access_token"])') \
          && curl -sf -X DELETE -H "Authorization: Bearer $TOKEN" \
               "https://management.azure.com/subscriptions/$SUB/resourcegroups/$RG?api-version=2021-04-01" \
          && { logger -t bench "deleting resource group $RG"; exit 0; }
        sleep 30
      done
      logger -t bench "self-destruct FAILED for $RG - delete it manually (infra/cleanup.sh)"
      shutdown -h now

runcmd:
  # Hard kill switch, independent of whether the benchmark succeeds or hangs.
  - [systemd-run, --unit=bench-watchdog, --on-active=__MAX_HOURS__h, /usr/local/bin/bench-selfdestruct]
  - [bash, -c, "set -a; . /etc/bench.env; git clone -q -b $BRANCH https://x-access-token:$GITHUB_TOKEN@github.com/$REPO.git /opt/bench && bash /opt/bench/infra/vm-entry.sh >> /var/log/bench.log 2>&1 || systemd-run --unit=bench-grace --on-active=${GRACE_MINUTES}m /usr/local/bin/bench-selfdestruct"]
