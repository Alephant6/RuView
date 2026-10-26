import os
import sys

from .service import main

# The dump holds health data: files readable by this user only.
os.umask(0o077)
sys.exit(main())
