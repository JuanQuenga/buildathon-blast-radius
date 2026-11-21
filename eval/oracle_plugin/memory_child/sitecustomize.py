"""Apply the optional address-space allowance to freshly spawned test interpreters."""
import os
import resource

value = os.environ.get("BR_CHILD_AS_MIB")
if value:
    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    limit = int(value) << 20
    if soft != resource.RLIM_INFINITY:
        limit = min(limit, soft)
    resource.setrlimit(resource.RLIMIT_AS, (limit, hard))
