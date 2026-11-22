# Green'Up Netatmo (add-on local Home Assistant)

Lit la borne Legrand Green'Up Home (module `NLEVSE`) via l'API Netatmo (60 s en charge, 5 min au repos)
et crée ses entités dans HA par découverte MQTT.

## Prérequis
- Add-on **Mosquitto broker** et intégration **MQTT** configurés.
- Mode **compte** (recommandé, seul à voir la borne NLEVSE) : `netatmo_email` et `netatmo_password`
  du compte Home + Control. Non officiel, lecture seule.
- Mode application (ne voit pas la borne à ce jour) : client_id, client_secret, refresh token.

## Installation
1. Copier le dossier `greenup_netatmo` dans `\\homeassistant\addons\` (add-on Samba share)
   ou dans `/addons/` (add-on Terminal/SSH).
2. Paramètres > Modules complémentaires > Boutique > menu ⋮ > **Rechercher des mises à jour**.
3. Section « Modules complémentaires locaux » > **Green'Up Netatmo** > Installer.
4. Onglet Configuration : coller client_id, client_secret, refresh_token. Laisser home_id
   et module_id vides (détection automatique). Activer `debug` pour le premier essai.
5. Démarrer, puis lire l'onglet Journal.

## Entités
| Entité | Unité | Usage |
|---|---|---|
| Puissance | W | instantané |
| Énergie session | kWh | remise à zéro à chaque charge |
| Énergie totale | kWh | compteur cumulé, **à mettre dans le tableau Énergie** |
| Durée de charge | min | |
| Statut / En charge | | 0 = au repos, 3 = en charge |
| Code erreur | | diagnostic |

Le refresh token est renouvelé automatiquement et stocké dans `/data/state.json`.
Si l'add-on signale un échec d'authentification, régénérez un token et recollez-le.
