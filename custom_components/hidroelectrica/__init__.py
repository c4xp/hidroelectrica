"""Punctul central al integrării Hidroelectrica România."""
import logging
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from .api_manager import ApiManager, ExpiredTokenError
from .const import DOMAIN

from .const import (
    DOMAIN,
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Configurare la adăugarea unei noi intrări de configurare."""
    hass.data.setdefault(DOMAIN, {})

    username = entry.data[CONF_USERNAME]
    password = entry.data[CONF_PASSWORD]
    update_interval = entry.options.get(
        CONF_UPDATE_INTERVAL,
        entry.data.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL),
    )

    _LOGGER.debug(
        "Configurare intrare pentru utilizator: %s cu interval: %s secunde",
        username,
        update_interval,
    )

    # Inițializăm managerul API
    api_manager = ApiManager(hass, username, password)
    try:
        await api_manager.async_login()
        _LOGGER.info("Autentificare reușită pentru utilizatorul %s.", username)
    except Exception as error:
        _LOGGER.error(
            "Eroare la autentificare pentru utilizatorul %s: %s",
            username,
            error,
        )
        return False

    # Inițializăm coordinatorul
    coordinator = HidroelectricaDataUpdateCoordinator(
        hass,
        api_manager,
        update_interval=timedelta(seconds=update_interval),
    )
    try:
        # Primul refresh (sincron cu startup)
        await coordinator.async_config_entry_first_refresh()
        _LOGGER.info(
            "Datele inițiale pentru utilizatorul %s au fost încărcate cu succes.",
            username,
        )
    except Exception as error:
        _LOGGER.error(
            "Eroare la actualizarea inițială a datelor pentru utilizatorul %s: %s",
            username,
            error,
        )
        return False

    # Stocăm referințele către manager și coordinator
    hass.data[DOMAIN][entry.entry_id] = {
        "api_manager": api_manager,
        "coordinator": coordinator,
    }

    # Configurăm platformele (ex. sensor)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # IMPORTANT: înregistrăm un update_listener
    # (astfel, dacă userul modifică opțiunile - intervalul -, se reîncarcă intrarea)
    entry.async_on_unload(entry.add_update_listener(update_listener))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Gestionăm ștergerea unei intrări de configurare."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        _LOGGER.info(
            "Intrarea de configurare %s a fost ștearsă cu succes.",
            entry.entry_id,
        )

    return unload_ok


async def update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Callback apelat când opțiunile intrării sunt modificate."""
    _LOGGER.debug(
        "Opțiuni actualizate pentru entry %s. Se reîncarcă intrarea.",
        entry.entry_id,
    )
    await hass.config_entries.async_reload(entry.entry_id)


class HidroelectricaDataUpdateCoordinator(DataUpdateCoordinator):
    """Coordinator pentru gestionarea și actualizarea datelor de la API-ul Hidroelectrica."""

    def __init__(
        self,
        hass: HomeAssistant,
        api_manager: ApiManager,
        update_interval: timedelta,
    ):
        """Inițializează coordinatorul."""
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=update_interval)
        self.api_manager = api_manager

    async def _async_update_data(self):
        """
        Actualizăm datele prin apelarea API-ului.
        Construim un dict cu informațiile colectate (self.data).
        """
        try:
            # 1. Obținem user_settings, cu re-login la nevoie
            try:
                user_settings = await self.api_manager._async_get_user_settings()
            except ExpiredTokenError:
                await self.api_manager.async_login()
                user_settings = await self.api_manager._async_get_user_settings()

            # Extragem conturile din user_settings (exemplu)
            account_number = user_settings["result"]["Data"]["Table1"][0]["AccountNumber"]
            utility_account_number = user_settings["result"]["Data"]["Table1"][0]["UtilityAccountNumber"]
            meter_number = user_settings["result"]["Data"]["Table1"][0].get("MeterNumber")

            # 2. Factura curentă
            current_bill = None
            if account_number and utility_account_number:
                try:
                    current_bill = await self.api_manager._async_get_bill(account_number, utility_account_number)
                except ExpiredTokenError:
                    await self.api_manager.async_login()
                    current_bill = await self.api_manager._async_get_bill(account_number, utility_account_number)

            # 3. Istoric facturi (interval din ultimul an până azi)
            bill_history = None
            if account_number and utility_account_number:
                today = datetime.now()
                one_year_ago = today - timedelta(days=365)
                from_date = one_year_ago.strftime("%Y-%m-%d")
                to_date = today.strftime("%Y-%m-%d")

                try:
                    bill_history = await self.api_manager._async_get_bill_history(
                        account_number,
                        utility_account_number,
                        from_date,
                        to_date,
                    )
                except ExpiredTokenError:
                    await self.api_manager.async_login()
                    bill_history = await self.api_manager._async_get_bill_history(
                        account_number,
                        utility_account_number,
                        from_date,
                        to_date,
                    )

            # 4. Info contoare
            multi_meter = None
            if account_number and utility_account_number:
                try:
                    multi_meter = await self.api_manager._async_get_multi_meter(
                        account_number,
                        utility_account_number,
                    )
                except ExpiredTokenError:
                    await self.api_manager.async_login()
                    multi_meter = await self.api_manager._async_get_multi_meter(
                        account_number,
                        utility_account_number,
                    )

            # 5. Consum
            usage_generation = None
            if meter_number:
                try:
                    usage_generation = await self.api_manager._async_get_usage_generation(meter_number)
                except ExpiredTokenError:
                    await self.api_manager.async_login()
                    usage_generation = await self.api_manager._async_get_usage_generation(meter_number)

            # Construim un dict cu TOT ce am colectat
            data = {
                "user_settings": user_settings,
                "current_bill": current_bill,
                "bill_history": bill_history,
                "multi_meter": multi_meter,
                "usage_generation": usage_generation,
            }

            # Verificăm status_code în toate datele colectate
            # Notă: unii parametri pot fi None (ex. current_bill dacă nu avem cont),
            # deci filtrăm cu "if datum" înainte de a verifica status_code.
            all_200 = all(
                (datum.get("status_code") == 200)
                for datum in data.values()
                if datum
            )
            if all_200:
                _LOGGER.debug("Datele actualizate: OK")
            else:
                _LOGGER.error(
                    "Eroare la actualizare: unele răspunsuri nu au status_code 200. Date: %s",
                    data,
                )

            return data

        except Exception as error:
            _LOGGER.error("Eroare la actualizarea datelor: %s", error)
            # Re-raise, pentru că DataUpdateCoordinator are nevoie să știe de excepție
            raise
