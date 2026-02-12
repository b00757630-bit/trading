# -*- coding: utf-8 -*-
"""
Surveillance BTC/USDT (4H) - API publique Binance via CCXT.
Stratégie :
  - Filtre Daily : SuperTrend (10, 3) ; pas de trade si Short.
  - Signal 4H : Prix > EMA 50 + crossover RSI > 45.
  - Sortie : Trailing Stop = close - (3 * ATR(14)), pas de TP fixe.
  - Persistance : suivi du trade ouvert dans le CSV, mise à jour de Current_SL à chaque bougie 4H.
Journal CSV + notification Telegram. Aucun credential privé (lecture seule).
"""

import asyncio
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

import ccxt
import pandas as pd
import pandas_ta as ta
from dotenv import load_dotenv

# -----------------------------------------------------------------------------
# Constantes
# -----------------------------------------------------------------------------
SYMBOL = "BTC/USDT"
TIMEFRAME_4H = "4h"
TIMEFRAME_1D = "1d"
EMA_SLOW = 50
RSI_LENGTH = 14
RSI_CROSS_LEVEL = 45
SUPERTREND_LENGTH = 10
SUPERTREND_MULTIPLIER = 3
ATR_LENGTH = 14
ATR_TRAILING_MULTIPLIER = 3
SL_CANDLES = 3
LOOKBACK_4H = 120
LOOKBACK_1D = 250

# Gestion du risque (1% de 5000€ = 50€) — surchargeables par variables d'environnement
CAPITAL_EUR = int(os.getenv("CAPITAL_EUR", "500"))
RISQUE_POURCENT = float(os.getenv("RISQUE_POURCENT", "0.01"))
_default_risque = int(CAPITAL_EUR * RISQUE_POURCENT)
RISQUE_EUROS = int(os.getenv("RISQUE_EUROS", str(_default_risque)))

SCRIPT_DIR = Path(__file__).resolve().parent
# Charger .env tôt pour que CAPITAL_EUR / RISQUE_* / TELEGRAM_* soient disponibles
load_dotenv(SCRIPT_DIR / ".env")
CSV_PATH = SCRIPT_DIR / "journal_trading.csv"

# Colonnes CSV (avec Current_SL pour le trailing)
CSV_COLUMNS = [
    "Date", "Type", "Prix_Entree", "SL", "Current_SL", "TP", "Taille_Position",
    "Risque_Euros", "PnL_Theorique_Perdant", "Statut",
]

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def get_exchange() -> ccxt.Exchange:
    """Retourne une instance CCXT Binance (API publique, pas de clés)."""
    exchange = ccxt.binance({"options": {"defaultType": "spot"}})
    exchange.load_markets()
    return exchange


def get_indicators(
    exchange: ccxt.Exchange,
) -> Tuple[pd.DataFrame, pd.DataFrame, Optional[float], bool]:
    """
    Récupère les OHLCV 4H et 1D, calcule EMA 50, RSI(14), ATR(14) en 4H et SuperTrend (10,3) en Daily.

    Returns:
        (df_4h, df_1d, prix_actuel, ok)
    """
    try:
        ohlcv_4h = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME_4H, limit=LOOKBACK_4H)
        df_4h = pd.DataFrame(
            ohlcv_4h,
            columns=["timestamp", "open", "high", "low", "close", "volume"],
        )
        df_4h["timestamp"] = pd.to_datetime(df_4h["timestamp"], unit="ms")
        df_4h.set_index("timestamp", inplace=True)
        close_4h = df_4h["close"]

        ohlcv_1d = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME_1D, limit=LOOKBACK_1D)
        df_1d = pd.DataFrame(
            ohlcv_1d,
            columns=["timestamp", "open", "high", "low", "close", "volume"],
        )
        df_1d["timestamp"] = pd.to_datetime(df_1d["timestamp"], unit="ms")
        df_1d.set_index("timestamp", inplace=True)

        # Indicateurs 4H
        df_4h["ema50"] = ta.ema(close_4h, length=EMA_SLOW)
        df_4h["rsi"] = ta.rsi(close_4h, length=RSI_LENGTH)
        df_4h["atr"] = ta.atr(df_4h["high"], df_4h["low"], close_4h, length=ATR_LENGTH)

        # SuperTrend (10, 3) Daily
        st = ta.supertrend(
            df_1d["high"], df_1d["low"], df_1d["close"],
            length=SUPERTREND_LENGTH, multiplier=SUPERTREND_MULTIPLIER,
        )
        if st is not None and not st.empty:
            dir_col = [c for c in st.columns if c.startswith("SUPERTd_")]
            if dir_col:
                df_1d["supertrend_dir"] = st[dir_col[0]].values

        prix_actuel = float(close_4h.iloc[-1])
        return df_4h, df_1d, prix_actuel, True

    except Exception as e:
        logger.exception("get_indicators: %s", e)
        return pd.DataFrame(), pd.DataFrame(), None, False


def is_supertrend_daily_long(df_1d: pd.DataFrame) -> bool:
    """True si le SuperTrend (10, 3) Daily est Long (haussier) sur la dernière bougie."""
    if df_1d.empty or "supertrend_dir" not in df_1d.columns:
        return False
    if len(df_1d) < SUPERTREND_LENGTH + 2:
        return False
    last_dir = df_1d["supertrend_dir"].iloc[-1]
    return last_dir == 1


def check_signal(
    df_4h: pd.DataFrame,
    df_1d: pd.DataFrame,
    prix_actuel: float,
) -> bool:
    """
    Filtre Daily : SuperTrend (10, 3) doit être Long (pas de trade si Short).
    Signal 4H : prix > EMA 50 + crossover RSI > 45 sur la dernière bougie.
    """
    if df_4h.empty or df_1d.empty or prix_actuel is None:
        return False

    if not is_supertrend_daily_long(df_1d):
        logger.info("Filtre Daily: SuperTrend (10,3) non Long - pas de signal")
        return False

    if len(df_4h) < max(EMA_SLOW, RSI_LENGTH) + SL_CANDLES + 2:
        return False

    last = df_4h.iloc[-1]
    prev = df_4h.iloc[-2]
    close = last["close"]
    ema50 = last["ema50"]
    trend_ok = close > ema50
    if not trend_ok:
        logger.info("Pas de signal: close %.2f <= EMA50 %.2f", close, ema50)
        return False

    # Crossover RSI au-dessus de 45
    constant_45 = pd.Series(RSI_CROSS_LEVEL, index=df_4h.index)
    rsi_cross = ta.crossover(df_4h["rsi"], constant_45)
    crossover_ok = pd.notna(rsi_cross.iloc[-1]) and rsi_cross.iloc[-1] is True
    if not crossover_ok:
        logger.info("Pas de signal: pas de crossover RSI > 45 (RSI=%.1f)", last["rsi"])
        return False

    logger.info(
        "Signal détecté: SuperTrend Daily Long, close > EMA50, RSI croise > 45 (RSI=%.1f)",
        last["rsi"],
    )
    return True


def compute_trade_values(prix_entree: float, df_4h: pd.DataFrame) -> Optional[dict]:
    """
    SL initial = plus bas des 3 dernières bougies 4H. Pas de TP (trailing stop).
    Taille = risque 1% / (prix_entree - sl).
    """
    if df_4h.empty or len(df_4h) < SL_CANDLES:
        return None
    last_3 = df_4h.iloc[-SL_CANDLES:]
    sl_initial = float(last_3["low"].min())
    if prix_entree <= sl_initial:
        logger.warning("SL initial (low 3 bougies) >= prix entree, trade ignoré")
        return None
    risque_euros = RISQUE_EUROS
    taille_position = risque_euros / (prix_entree - sl_initial)
    pnl_perdant = (sl_initial - prix_entree) * taille_position
    return {
        "Prix_Entree": round(prix_entree, 2),
        "SL": round(sl_initial, 2),
        "Current_SL": round(sl_initial, 2),
        "TP": None,
        "Taille_Position": round(taille_position, 8),
        "Risque_Euros": round(risque_euros, 2),
        "PnL_Theorique_Perdant": round(pnl_perdant, 2),
    }


def get_open_trade_from_csv() -> Optional[dict]:
    """Retourne le dernier trade avec Statut=OPEN (ou None)."""
    if not CSV_PATH.exists():
        return None
    try:
        df = pd.read_csv(CSV_PATH, encoding="utf-8")
        if df.empty:
            return None
        open_rows = df[df["Statut"].astype(str).str.strip().str.upper() == "OPEN"]
        if open_rows.empty:
            return None
        last = open_rows.iloc[-1].to_dict()
        for k in ["Prix_Entree", "SL", "Current_SL", "Taille_Position", "Risque_Euros", "PnL_Theorique_Perdant"]:
            if k in last and last[k] is not None and str(last[k]).strip() != "":
                try:
                    last[k] = float(last[k])
                except (TypeError, ValueError):
                    pass
        if last.get("Current_SL") is None or (isinstance(last.get("Current_SL"), float) and pd.isna(last.get("Current_SL"))):
            last["Current_SL"] = last.get("SL")
        return last
    except Exception as e:
        logger.warning("Lecture CSV: %s", e)
        return None


def update_csv_new_trade(trade_row: dict) -> None:
    """Ajoute une nouvelle ligne au journal (nouveau signal)."""
    row = {
        "Date": trade_row.get("Date", datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")),
        "Type": "Long",
        "Prix_Entree": trade_row["Prix_Entree"],
        "SL": trade_row["SL"],
        "Current_SL": trade_row["Current_SL"],
        "TP": "" if trade_row.get("TP") is None else trade_row["TP"],
        "Taille_Position": trade_row["Taille_Position"],
        "Risque_Euros": trade_row["Risque_Euros"],
        "PnL_Theorique_Perdant": trade_row["PnL_Theorique_Perdant"],
        "Statut": "OPEN",
    }
    df = pd.DataFrame([row], columns=CSV_COLUMNS)
    file_exists = CSV_PATH.exists()
    df.to_csv(
        CSV_PATH,
        mode="a",
        header=not file_exists,
        index=False,
        encoding="utf-8",
    )
    logger.info("Nouveau trade ajouté au journal: %s", CSV_PATH)


def update_csv_open_trade(current_sl: float, statut: str = "OPEN") -> None:
    """
    Met à jour le dernier trade OPEN dans le CSV : Current_SL et éventuellement Statut.
    statut = "OPEN" pour simple mise à jour du trailing, "CLOSED_SL" pour clôture.
    """
    if not CSV_PATH.exists():
        return
    try:
        df = pd.read_csv(CSV_PATH, encoding="utf-8")
        if df.empty:
            return
        idx_open = df[df["Statut"].astype(str).str.strip().str.upper() == "OPEN"].index
        if len(idx_open) == 0:
            return
        last_open_idx = idx_open[-1]
        df.at[last_open_idx, "Current_SL"] = round(current_sl, 2)
        df.at[last_open_idx, "Statut"] = statut
        df.to_csv(CSV_PATH, index=False, encoding="utf-8")
        logger.info("Journal mis à jour: Current_SL=%.2f, Statut=%s", current_sl, statut)
    except Exception as e:
        logger.warning("Mise à jour CSV: %s", e)


async def send_telegram_message(message: str) -> bool:
    """Envoie un message Telegram via python-telegram-bot (API asynchrone)."""
    try:
        from telegram import Bot

        token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            logger.warning("TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID manquants, notification ignorée")
            return False
        bot = Bot(token=token)
        await bot.send_message(chat_id=chat_id, text=message)
        logger.info("Notification Telegram envoyée")
        return True
    except Exception as e:
        logger.exception("Erreur envoi Telegram: %s", e)
        return False


def build_telegram_message(trade: dict) -> str:
    """Construit le message pour Telegram (trailing stop, pas de TP fixe)."""
    return (
        "🟢 Signal LONG BTC/USDT (4H)\n"
        "────────────────────\n"
        f"📅 Date: {trade['Date']}\n"
        f"💰 Prix d'entrée: {trade['Prix_Entree']} USDT\n"
        f"🛑 SL initial: {trade['SL']} USDT\n"
        f"📈 Current_SL (trailing 3×ATR): {trade['Current_SL']} USDT\n"
        f"📐 Taille position: {trade['Taille_Position']} BTC\n"
        f"⚠️ Risque: {trade['Risque_Euros']} €\n"
        f"❌ PnL théorique (SL): {trade['PnL_Theorique_Perdant']} €\n"
        "────────────────────\n"
        "Sortie: Trailing Stop (close - 3×ATR), pas de TP fixe. Statut: OPEN"
    )


def run_cycle() -> None:
    """
    Une itération :
    - S'il existe un trade OPEN : mise à jour du Current_SL (trailing 3×ATR) à chaque bougie 4H,
      ou clôture si low <= Current_SL.
    - Sinon : détection signal (SuperTrend Daily Long + EMA50 + RSI cross 45), ajout CSV + Telegram.
    """
    exchange = get_exchange()
    df_4h, df_1d, prix_actuel, ok = get_indicators(exchange)
    if not ok or prix_actuel is None:
        logger.warning("Cycle ignoré: données invalides")
        return

    open_trade = get_open_trade_from_csv()

    if open_trade is not None:
        # Suivi du trade ouvert : dernière bougie 4H close + ATR pour trailing
        if df_4h.empty or len(df_4h) < 2:
            return
        last = df_4h.iloc[-1]
        current_sl = float(open_trade.get("Current_SL", open_trade.get("SL", 0)))
        low = last["low"]
        close = last["close"]
        atr_val = last.get("atr")
        if pd.isna(atr_val) or atr_val <= 0:
            return
        # Sortie : pendant la bougie, si low <= current_sl → clôture
        if low <= current_sl:
            update_csv_open_trade(current_sl, "CLOSED_SL")
            logger.info("Trade fermé (Trailing Stop / SL): low=%.2f <= Current_SL=%.2f", low, current_sl)
            message = (
                "🚨 **SORTIE DE TRADE - BTC/USDT**\n"
                f"Le prix ({low}) a touché le Trailing Stop ({current_sl}).\n\n"
                "👉 Ferme ta position manuellement sur l'exchange !"
            )
            asyncio.run(send_telegram_message(message))
            return
        # Remonter le trailing : close - 3*ATR (ne monte jamais)
        candidate_sl = float(close - ATR_TRAILING_MULTIPLIER * atr_val)
        entry = float(open_trade["Prix_Entree"])
        old_sl = current_sl
        if candidate_sl > current_sl and candidate_sl < close:
            current_sl = round(candidate_sl, 2)
            logger.info("Trailing SL mis à jour: %.2f (close 4H=%.2f, ATR=%.2f)", current_sl, close, atr_val)
            # Notification Telegram uniquement si le nouveau SL est > 0,5 % au-dessus de l'ancien
            if old_sl > 0 and (current_sl - old_sl) / old_sl > 0.005:
                msg = (
                    "📈 MAJ Trailing Stop BTC\n"
                    f"Ancien SL : {old_sl}\n"
                    f"Nouveau SL : {current_sl}\n"
                    f"Prix actuel : {prix_actuel}"
                )
                asyncio.run(send_telegram_message(msg))
        update_csv_open_trade(current_sl, "OPEN")
        return

    if not check_signal(df_4h, df_1d, prix_actuel):
        logger.info("Aucun signal (prix=%.2f)", prix_actuel)
        return

    trade = compute_trade_values(prix_actuel, df_4h)
    if trade is None:
        return
    trade["Date"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    update_csv_new_trade(trade)
    message = build_telegram_message(trade)
    asyncio.run(send_telegram_message(message))


def main_loop(interval_seconds: int = 4 * 3600) -> None:
    """
    Boucle principale : exécution toutes les 4 heures.
    Gestion des exceptions pour éviter l'arrêt en cas de coupure internet ou erreur API.
    """
    logger.info("Démarrage surveillance BTC/USDT (4H) - intervalle %s s", interval_seconds)
    while True:
        try:
            run_cycle()
        except Exception as e:
            logger.exception("Erreur dans le cycle (script continue): %s", e)
        logger.info("Prochaine exécution dans %d secondes", interval_seconds)
        time.sleep(interval_seconds)


if __name__ == "__main__":
    # Option : une seule exécution pour tests
    if len(sys.argv) > 1 and sys.argv[1] == "--once":
        run_cycle()
    else:
        main_loop(interval_seconds=4 * 3600)
