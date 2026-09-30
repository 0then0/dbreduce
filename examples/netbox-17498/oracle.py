"""NetBox #17498: the real CSV form must raise the target exception."""

import json
import os
import sys
from contextlib import redirect_stdout

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "netbox.settings")
with redirect_stdout(sys.stderr):
    django.setup()

from dcim.forms.bulk_import import DeviceTypeImportForm
from dcim.models import Manufacturer

verdict = {"reproduced": False}
if Manufacturer.objects.filter(description="d").count() >= 2:
    form = DeviceTypeImportForm(
        data={"manufacturer": "d", "model": "m1", "slug": "m1", "u_height": "1"},
        headers={"manufacturer": "description", "model": None, "slug": None, "u_height": None},
    )
    try:
        form.is_valid()
    except Manufacturer.MultipleObjectsReturned:
        verdict = {
            "reproduced": True,
            "signature": "netbox-17498-manufacturer-description-multiple-objects",
        }
    # Unknown exceptions propagate; they are not validity evidence or a negative verdict.
print(json.dumps(verdict))
