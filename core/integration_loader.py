import importlib
import pkgutil
import os
from core.ijarvis_integration import IJarvisIntegration

def load_all_integrations():
    integrations = []
    package_path = os.path.join(os.path.dirname(__file__), "../jarvis_integrations")
    package_path = os.path.abspath(package_path)

    for _, name, _ in pkgutil.iter_modules([package_path]):
        module = importlib.import_module(f"jarvis_integrations.{name}")
        for attr in dir(module):
            obj = getattr(module, attr)
            if isinstance(obj, type) and issubclass(obj, IJarvisIntegration) and obj is not IJarvisIntegration:
                integrations.append(obj())
    return integrations 