"""
Module azure_api containing the AzureKeyVault class,
which is responsible for Azure resource management and Azure client handling mechanism.
"""
import time
from pathlib import Path
from typing import List

from msrestazure.azure_exceptions import CloudError
from nnstorm_cloud.azure.api import AzureApi

from azure.keyvault.secrets import SecretClient
from azure.mgmt.keyvault import KeyVaultManagementClient
from azure.mgmt.keyvault.models import NetworkRuleSet, VirtualNetworkRule


class AzureKeyVault(AzureApi):
    """Azure KeyVault API to set/get secrets from a given Keyvault on Azure"""

    def __init__(self, keyvault_name: str = None, auth_path: Path = None):
        """Initialize the Azure Keyvault client

        Args:
            keyvault_name (str, optional): Name of the keyvault. Defaults to None.
            auth_path (Path, optional): Authentication json file path. Defaults to None.
        """
        super(AzureKeyVault, self).__init__(auth_path)

        self.name = keyvault_name
        self.uri = f"https://{self.name}.vault.azure.net"

        self.secret_client = SecretClient(vault_url=self.uri, credential=self.credentials, version="7.0")
        self.keyvault_client = self.client(KeyVaultManagementClient)

        self.logger.debug(f"Keyvault manager ready: {self.name}")

    def get_secret(self, name: str) -> str:
        """Get secret from keyvault

        Args:
            name (str): name of the secret to get from keyvault

        Returns:
            str: The secret value
        """
        try:
            secret = self.secret_client.get_secret(name)
            self.logger.debug(f"Retrieved secret: {secret.id}")
        except CloudError as e:
            self.logger.error(f"Could not get secret: {name}")
            raise e
        return secret.value

    def set_secret(self, name: str, value: str) -> str:
        """Set secret value in keyvault

        Args:
            name (str): name of the secret to get from keyvault
            value (str): value of the keyvault secret
        """
        try:
            secret = self.secret_client.set_secret(name, value)
            self.logger.debug(f"Set secret: {secret.id}")
        except CloudError as e:
            self.logger.error(f"Could not set secret: {name}")
            raise e
        return secret.value

    def delete_secret(self, name: str, purge: bool = True) -> None:
        """Delete a secret from the key vault

        Args:
            name (str): name of the secret
            purge (bool, optional): whether to purge the secret or soft-delete. Defaults to True.
        """
        poller = self.secret_client.begin_delete_secret(name)
        poller.wait()
        if purge:
            self.secret_client.purge_deleted_secret(name)

    def grant_access(self, rsg: str, subnet_ids: List[str] = None) -> None:
        """Grant access to a keyvault from a list of subnets

        Args:
            rsg (str): Resource group of the key vault
            subnet_ids (List[str], optional): list of subnet IDs. Defaults to None.
        """
        self.logger.info(f"Grant access to KeyVault running for: {self.name}")
        tenant_id = self._get_tenant_id()

        vault = self.keyvault_client.vaults.get(rsg, self.name)
        props = vault.properties
        tenant_update = False

        if (
            tenant_id not in [x.tenant_id for x in vault.properties.access_policies]
        ) or vault.properties.tenant_id != tenant_id:
            tenant_update = True
            props.tenant_id = tenant_id
            props.access_policies.append(
                {
                    "tenant_id": tenant_id,
                    "object_id": self.get_object_id(),
                    "permissions": {"secrets": ["all"]},
                }
            )
        if subnet_ids:
            props.network_acls = NetworkRuleSet(
                default_action="Deny",
                ip_rules=[],
                virtual_network_rules=[VirtualNetworkRule(id=i) for i in subnet_ids],
            )
        if subnet_ids or tenant_update:
            kv = self.keyvault_client.vaults.create_or_update(
                rsg,
                self.name,
                {"location": vault.location, "properties": props},
            )
            kv.wait()

    def delete_keyvault(self, rsg: str, location: str, purge: bool = True) -> None:
        """Delete a key vault

        Args:
            rsg (str): resource group of the vault
            location (str): location of the vault
            purge (bool, optional): whether to purge the vault. Defaults to True.
        """
        self.logger.info(f"Deleting keyvault {self.name}")
        try:
            d = self.keyvault_client.vaults.delete(rsg, self.name)
            d.wait()
            if purge:
                self.logger.info(f"Purging keyvault {self.name}")
                p = self.keyvault_client.vaults.purge_deleted(self.name, location)
                p.wait()
        except:
            pass

    def create_keyvault(self, soft_delete: bool = True, subnet_ids: List[str] = None):
        """Creates a key vault object in Azure

        Args:
            soft_delete (bool, optional): turn on soft-delete. Defaults to True.
            subnet_ids (List[str], optional): subnet IDs to grant access to. Defaults to None.

        Raises:
            RuntimeError: Keyvault name is already taken.
        """
        self.logger.info(f"Create or update KeyVault running for: {self.name}")

        tenant_id = self._get_tenant_id()
        keyvault_client = self.client(KeyVaultManagementClient)

        if not self.check_keyvault_available(self.keyvault.name):
            raise RuntimeError("Keyvault name is taken by deleted keyvault.")

        configuration = {
            "location": self.get_location(),
            "properties": {
                "sku": {"name": "standard"},
                "tenant_id": tenant_id,
                "enable_soft_delete": soft_delete,
                "access_policies": [
                    {
                        "tenant_id": tenant_id,
                        "object_id": self.get_object_id(),
                        "permissions": {"keys": ["all"], "secrets": ["all", "purge"]},
                    }
                ],
            },
        }

        if subnet_ids:
            configuration["properties"]["network_acls"] = NetworkRuleSet(
                default_action="Deny", ip_rules=[], virtual_network_rules=[VirtualNetworkRule(id=i) for i in subnet_ids]
            )

        kv = keyvault_client.vaults.create_or_update(self.rsg, self.name, configuration)
        kv.wait()

        while True:
            try:
                self.set_secret("test", "x")
            except:
                self.logger.warning("Waiting for keyvault to come up. Please check connection to the VNET.")
                time.sleep(1)
            else:
                self.keyvault.delete_secret("test")
                break

    def check_keyvault_available(self, name: str) -> bool:
        """Check if keyvault name is available

        Args:
            name (str): name of the keyvault

        Returns:
            bool: whether the name is available or not
        """
        keyvault_client = self.client(KeyVaultManagementClient)
        deleted_vaults = keyvault_client.vaults.list_deleted()
        names = [i.name for i in deleted_vaults]
        available = keyvault_client.vaults.check_name_availability(name)
        if (name in names) or not available.name_available:
            return False
        return True