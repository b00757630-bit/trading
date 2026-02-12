# BotTrading – Surveillance BTC/USDT (4H)

Script de surveillance **BTC/USDT** sur le timeframe 4H, utilisant l’**API publique Binance** via CCXT. Détection de signaux longs, journalisation en CSV et notifications Telegram. **Aucune clé API Binance** n’est requise (lecture seule).

---

## Stratégie

### Filtre directionnel (Daily)
- **SuperTrend (10, 3)** sur le timeframe **Daily** : la tendance doit être **haussier** (indicateur au vert). Sinon, aucun signal long n’est émis.

### Signal long (4H)
- **Tendance** : prix de clôture au-dessus de l’**EMA 50** (4H).
- **Déclencheur** : **RSI(14)** croise à la hausse le niveau **45** (crossover RSI vs 45) sur la dernière bougie 4H.

### Gestion de la sortie (pas de Take Profit fixe)
- **Stop Loss initial** : plus bas (Low) des **3 dernières bougies 4H**.
- **Trailing Stop** : **3 × ATR(14)** en 4H. À chaque **nouvelle clôture** de bougie 4H, le SL est recalculé : `close − 3×ATR`. Il **monte** si ce niveau est plus haut que l’actuel, et **ne redescend jamais**.
- **Take Profit** : **aucun** ; la sortie se fait uniquement par touche du trailing stop (ou du SL initial).

Lorsqu’un signal est détecté, une ligne est ajoutée au journal `journal_trading.csv` et un message détaillé est envoyé sur Telegram (si configuré).

---

## Prérequis

- Python 3.10+
- Connexion internet

---

## Installation

```bash
cd BotTrading
python -m venv .venv
.venv\Scripts\activate   # Windows
# source .venv/bin/activate   # Linux / macOS
pip install -r requirements.txt
```

### Configuration (optionnelle)

Pour les notifications Telegram :

1. Copier le fichier d’exemple :  
   `copy .env.example .env` (Windows) ou `cp .env.example .env` (Linux/macOS)
2. Créer un bot avec [@BotFather](https://t.me/BotFather) et récupérer le **token**.
3. Renseigner dans `.env` :
   - `TELEGRAM_BOT_TOKEN=` votre token
   - `TELEGRAM_CHAT_ID=` votre chat ID (obtenir via `https://api.telegram.org/bot<TOKEN>/getUpdates` après avoir envoyé un message au bot)

Sans `.env` ou sans token/chat_id, le script tourne normalement mais n’envoie pas de notification Telegram.

---

## Utilisation

**Lancer la surveillance en continu** (une analyse toutes les 4 heures) :

```bash
python btc_surveillance.py
```

**Une seule exécution** (test) :

```bash
python btc_surveillance.py --once
```

---

## Journal CSV (`journal_trading.csv`)

À chaque signal, une ligne est **ajoutée** (sans écraser le fichier) avec les colonnes :

| Colonne | Description |
|--------|-------------|
| Date | Date/heure du signal (UTC) |
| Type | Long |
| Prix_Entree | Prix d’entrée (USDT) |
| SL | Stop loss initial (USDT), puis trailing 3×ATR |
| TP | Non utilisé (sortie par trailing stop) |
| Taille_Position | Quantité en BTC |
| Risque_Euros | 1 % du capital (ex. 50 € pour 5000 €) |
| PnL_Theorique_Gagnant | Non applicable (pas de TP fixe) |
| PnL_Theorique_Perdant | Perte potentielle si SL atteint (€) |
| Statut | OPEN |

Formules utilisées :
- **Risque_Euros** = 1 % du capital
- **Taille_Position** = Risque_Euros / (Prix_Entree - SL_initial)
- **PnL_Theorique_Perdant** = (SL - Prix_Entree) × Taille_Position  

Le SL initial est le plus bas des 3 dernières bougies 4H ; il est ensuite remonté à chaque clôture 4H selon la règle **trailing 3×ATR** (il ne redescend jamais).

---

## Déploiement sur GitHub Actions

Le workflow `.github/workflows/main.yml` exécute **une fois par heure** (cron) un cycle de surveillance (`python btc_surveillance.py --once`) sur un runner Ubuntu avec Python 3.10. Tu peux aussi lancer le workflow à la main via l’onglet **Actions** → **BTC Surveillance** → **Run workflow**.

### Ajouter les secrets dans le dépôt GitHub

Pour que les notifications Telegram fonctionnent en CI, enregistre les variables en tant que **secrets** du dépôt (elles ne sont jamais affichées ni loguées) :

1. Ouvre ton dépôt sur **GitHub**.
2. Va dans **Settings** (Paramètres) du dépôt.
3. Dans le menu de gauche : **Secrets and variables** → **Actions**.
4. Clique sur **New repository secret**.
5. Crée deux secrets :
   - **Nom** : `TELEGRAM_BOT_TOKEN` → **Value** : le token de ton bot (ex. `123456:ABC-DEF...`).
   - **Nom** : `TELEGRAM_CHAT_ID` → **Value** : ton Chat ID (ex. `6547312430`).

Le workflow injecte ces secrets dans l’environnement du job ; le script les lit via `os.getenv("TELEGRAM_BOT_TOKEN")` et `os.getenv("TELEGRAM_CHAT_ID")`. Ne mets **jamais** ces valeurs dans le code ni dans un fichier versionné.

---

## Structure du projet

```
BotTrading/
├── .github/workflows/main.yml   # CI : exécution horaire
├── btc_surveillance.py         # Script principal
├── journal_trading.csv   # Créé automatiquement (signaux)
├── requirements.txt
├── .env.example
├── .env                  # À créer (optionnel, Telegram)
├── .gitignore
└── README.md
```

---

## Dépendances

- **ccxt** – API Binance (publique)
- **pandas** – Données OHLCV
- **pandas-ta** – Indicateurs (EMA, SMA, RSI)
- **python-dotenv** – Variables d’environnement
- **python-telegram-bot** – Envoi des notifications

---

## Robustesse

La boucle principale est protégée par un `try/except` : en cas d’erreur (coupure internet, timeout API, etc.), l’exception est loguée et le script **reprend au cycle suivant** (4 h plus tard) au lieu de s’arrêter.
