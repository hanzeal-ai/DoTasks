#!/usr/bin/python3.11
"""Unprivileged forced command; only the fixed receiver may run with sudo."""
import os
import re
import sys

command = os.environ.get('SSH_ORIGINAL_COMMAND', '')
if not re.fullmatch(r'site [a-f0-9]{40}', command):
    sys.exit('Only a site release with an exact commit SHA is accepted')
os.execv('/usr/bin/sudo', ['sudo', '-n', '/usr/local/libexec/dotasks-deploy/receive.py', command])
