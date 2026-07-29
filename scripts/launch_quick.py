#!/usr/bin/env python3
"""Quick Tencent VPS launch — uses existing VPC + key in ap-singapore."""
import json, os, sys
from tencentcloud.common import credential
from tencentcloud.cvm.v20170312 import cvm_client, models as cvm_models

SECRET_ID = os.getenv("TENCENT_SECRET_ID", "IKIDX7qfPD2yRtWop3Nk6BDHxPRXceZcsDXm")
SECRET_KEY = os.getenv("TENCENT_SECRET_KEY", "eN8OpOVG9f5Y4PIKLM8530ixluSxhq5d")
REGION = "ap-singapore"

cred = credential.Credential(SECRET_ID, SECRET_KEY)
cvm = cvm_client.CvmClient(cred, REGION)

# Try multiple instance types in order of preference
INSTANCE_TYPES = [
    "SA5.SMALL2",   # 2c/2g
    "SA2.SMALL2",   # 2c/2g
    "S5.SMALL2",    # 2c/2g
    "SA5.SMALL1",   # 1c/1g
    "S6.SMALL2",    # 2c/2g
    "SA2.SMALL1",   # 1c/1g
    "SA3.SMALL2",   # 2c/4g
]

ZONES = ["ap-singapore-2", "ap-singapore-3", "ap-singapore-4", "ap-singapore-1"]

for zone in ZONES:
    for itype in INSTANCE_TYPES:
        print(f"Trying {itype} in {zone}...")
        req = cvm_models.RunInstancesRequest()
        req.InstanceChargeType = "POSTPAID_BY_HOUR"
        req.Placement = cvm_models.Placement()
        req.Placement.Zone = zone
        req.InstanceType = itype
        req.ImageId = "img-mmytdhbn"  # Ubuntu 24.04
        req.VirtualPrivateCloud = cvm_models.VirtualPrivateCloud()
        req.VirtualPrivateCloud.VpcId = "vpc-dd5lvkd7"
        req.VirtualPrivateCloud.SubnetId = "subnet-qxk4wo98"
        req.InternetAccessible = cvm_models.InternetAccessible()
        req.InternetAccessible.InternetMaxBandwidthOut = 5
        req.InternetAccessible.PublicIpAssigned = True
        req.InternetAccessible.InternetChargeType = "TRAFFIC_POSTPAID_BY_HOUR"
        req.InstanceCount = 1
        req.InstanceName = "opsora-api-sg"
        req.LoginSettings = cvm_models.LoginSettings()
        req.LoginSettings.KeyIds = ["skey-ngbla6nn"]

        try:
            resp = cvm.RunInstances(req)
            data = json.loads(resp.to_json_string())
            ids = data.get("InstanceIdSet", [])
            if ids:
                print(f"\n✅ SUCCESS! Instance: {ids[0]}")
                print(f"   Type: {itype}, Zone: {zone}")
                print(f"   Waiting for IP...")
                import time
                time.sleep(10)
                # Get IP
                desc = cvm_models.DescribeInstancesRequest()
                desc.InstanceIds = ids
                r2 = cvm.DescribeInstances(desc)
                d2 = json.loads(r2.to_json_string())
                for inst in d2.get("InstanceSet", []):
                    pub_ips = inst.get("PublicIpAddresses", [])
                    priv_ips = inst.get("PrivateIpAddresses", [])
                    print(f"   Public IP:  {pub_ips[0] if pub_ips else 'pending'}")
                    print(f"   Private IP: {priv_ips[0] if priv_ips else 'pending'}")
                    print(f"   Status:     {inst.get('InstanceState', '?')}")
                    print(f"\n   SSH: ssh -i /root/.ssh/opsora-gpu-tencent.pem root@{pub_ips[0] if pub_ips else '<IP>'}")
                sys.exit(0)
        except Exception as e:
            import time as _t; _t.sleep(0.2)  # rate limit
            err = str(e)
            if "InsufficientBalance" in err:
                print(f"   ❌ InsufficientBalance — need to topup")
                sys.exit(1)
            elif "ResourceInsufficient" in err:
                print(f"   ⚠️ Not available in this zone")
            elif "ResourceUnavailable" in err:
                print(f"   ⚠️ Instance type not available")
            elif "InvalidParameterValue" in err:
                print(f"   ⚠️ Invalid param: {err[:100]}")
            else:
                print(f"   ❌ {err[:120]}")

print("\n❌ All combinations failed")
sys.exit(1)
