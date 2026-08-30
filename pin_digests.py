#!/usr/bin/env python3
"""wq#856: Pin all container images to their current running digests.
Reads docker images --digests to build a map, then patches compose files."""

import re, shutil, sys, os
from datetime import datetime

TIMESTAMP = datetime.now().strftime('%Y%m%d_%H%M%S')

# Registry digests from `docker images --digests` — these are the images
# currently pulled on this host, matching what's running.
DIGEST_MAP = {
    'actualbudget/actual-server:latest': 'sha256:e18b7fbfec6157a368fad4146563f397502e9da70a120aeaeac63b4977405d1c',
    'binwiederhier/ntfy': 'sha256:081b53dbb20674fcfe05fdb4eb8af9036a2645ef979543d16f7f80803af467b1',
    'binwiederhier/ntfy:latest': 'sha256:081b53dbb20674fcfe05fdb4eb8af9036a2645ef979543d16f7f80803af467b1',
    'clickhouse/clickhouse-server:24.3.3.102-alpine': 'sha256:f226fe41f0578968b7f68a54b902d203ff4decfddfccb97c89fe5bfc36a51b66',
    'ghcr.io/berriai/litellm:main-stable': 'sha256:6c82d338a60e7b273ae46bf1d8db814d2856ae010f96c44eeadde574d3893f76',
    'ghcr.io/danny-avila/librechat:latest': 'sha256:c5db3331b845e1f289f8d04c0c77936c4bbe372f76730a804abc1c37e44d23a9',
    'ghcr.io/plausible/community-edition:v2.1.4': 'sha256:4c2553516d09e3c7b1b9c39cca04a04c28c871f525adc8dbb7a2a8a20fed0857',
    'glanceapp/glance:latest': 'sha256:32ab73d80f2b8b5fb0735b0431deb36b93fbb6b2fb43592449b0178c8b83e350',
    'langfuse/langfuse:3': 'sha256:dd9874fb6a29aa6b3070754760876b42d0e440bb34537d7196d51bc15abb0068',
    'langfuse/langfuse-worker:3': 'sha256:5e7a487540a61b2c419e869684915839b412fa46aaaf0fd43022f25f8875ba4f',
    'langgenius/dify-api:1.14.2': 'sha256:062815df8ed6fcf82285e55f3cb5586241b2828e6695a3fb8114bfb99b5e8517',
    'langgenius/dify-plugin-daemon:0.6.1-local': 'sha256:fa7ad45e1b2a777cd243b902609abecb4fd924adcd13d21ef4258b5090a09be5',
    'langgenius/dify-sandbox:0.2.15': 'sha256:750e1111426ef31a9217b81c98cccfb750f17b182af3221102e420afa9f0928e',
    'langgenius/dify-web:1.14.2': 'sha256:db73434e185ac778f31f4cc1adcce0dbe84b41a95ea5af58216acf095eed5f67',
    'mongo:7': 'sha256:9bdaeb6dac6e7e762e84e2f84103d1f9bb078fa1ba6bde8bb9d2274f655ad173',
    'nginx:alpine': 'sha256:4a73073bd557c65b759505da037898b61f1be6cbcc3c2c3aeac22d2a470c1752',
    'nginx:latest': 'sha256:5a88c9c45479443d7be2eadc894b4ed0a9801bae03d97a5760ae13b5c2005942',
    'pgvector/pgvector:pg16': 'sha256:a36250871de0833b8757561c72f2477ef1ddd1101afa4e617fb552e0de514c6b',
    'postgres:15-alpine': 'sha256:3d0f7584ed7d04e27fa050d6683a74746608faf21f202be78460d679cc56461f',
    'postgres:16-alpine': 'sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777',
    'quay.io/minio/minio:latest': 'sha256:14cea493d9a34af32f524e538b8346cf79f3321eff8e708c1e2960462bd8936e',
    'redis:6-alpine': 'sha256:ec5e187c913d422cdf60f4216a5fdfb95246792c6de6fe21ff5bed75cbfc8c23',
    'redis:7-alpine': 'sha256:e7723ff73d963f5cc6d9c4643ea3d989527a402a319239054e9472a7fb9219a2',
    'semitechnologies/weaviate:1.27.0': 'sha256:53fa576934f8e2c9130dc3a61a0f9b7d9330fe0ff456ccb37db05d03b700abc9',
    'twinproduction/gatus:v5.10.0': 'sha256:881131a4c74bc8a83c09dc4eb83ef659885d5caf84baabdfcc94fcc13f9e39f6',
    'ubuntu:24.04': 'sha256:c4a8d5503dfb2a3eb8ab5f807da5bc69a85730fb49b5cfca2330194ebcc41c7b',
    'ubuntu/squid:latest': 'sha256:6a097f68bae708cedbabd6188d68c7e2e7a38cedd05a176e1cc0ba29e3bbe029',
    'busybox:latest': 'sha256:32015ee641bfecc97161986c9d24957068175444f66fbcbe08a664b6cf5c1c2e',
}

# Compose files with running containers (skip sim-studio, mem0, leantime, etc.)
COMPOSE_FILES = [
    '/home/lumen/docker/docker-compose.yml',
    '/home/lumen/langfuse/docker-compose.yml',
    '/home/lumen/plausible/docker-compose.yml',
    '/home/lumen/actual-budget/docker-compose.yml',
    '/home/lumen/glance/docker-compose.yml',
    '/home/lumen/sentinel/docker-compose.yml',
    '/home/lumen/dify/docker/docker-compose.yaml',
]

# Skip locally-built images and non-running images
SKIP_IMAGES = {'postgres-mcp:local'}

# Regex: matches `image: repo:tag` or `image: repo:tag@sha256:...` with optional comment
IMAGE_RE = re.compile(r'^(\s*image:\s*)(\S+?)(\s*(?:#.*)?)$')

def pin_image(img_ref):
    """Given an image reference (possibly already pinned), return the correctly pinned version."""
    # Strip any existing @sha256:... and trailing comment marker
    base = img_ref.split('@')[0]
    if base in SKIP_IMAGES:
        return None  # don't touch
    digest = DIGEST_MAP.get(base)
    if not digest:
        return None  # unknown image, leave alone
    return f"{base}@{digest}"

def process_file(path):
    if not os.path.exists(path):
        print(f"SKIP (missing): {path}")
        return 0
    
    bak = f"{path}.bak.{TIMESTAMP}_pre_digest_pin"
    shutil.copy2(path, bak)
    print(f"BACKUP: {bak}")
    
    with open(path) as f:
        lines = f.readlines()
    
    changes = 0
    new_lines = []
    for line in lines:
        m = IMAGE_RE.match(line)
        if m:
            prefix, img_ref, suffix = m.groups()
            pinned = pin_image(img_ref)
            if pinned and pinned != img_ref:
                # Build comment
                comment = f"  # wq#856: pinned {datetime.now().strftime('%Y-%m-%d')}"
                new_line = f"{prefix}{pinned}{comment}\n"
                print(f"  PIN: {img_ref} -> {pinned}")
                new_lines.append(new_line)
                changes += 1
                continue
        new_lines.append(line)
    
    if changes:
        with open(path, 'w') as f:
            f.writelines(new_lines)
        print(f"  WROTE {changes} pins to {path}")
    else:
        # Remove unnecessary backup
        os.remove(bak)
        print(f"  NO CHANGES: {path}")
    return changes

total = 0
for f in COMPOSE_FILES:
    print(f"\n=== {f} ===")
    total += process_file(f)

print(f"\n=== TOTAL: {total} image references pinned ===")
