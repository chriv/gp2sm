"""SmugMug API v2 adapter."""


def service():
    from gp2sm.services.registry import ServiceInfo
    from gp2sm.smugmug.client import SmugMugClient
    return ServiceInfo(name="smugmug", kind="destination",
                       factory=lambda config: SmugMugClient.from_config_file(config),
                       description="SmugMug (sign in with `gp2sm auth smugmug`)")
