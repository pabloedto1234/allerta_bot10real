import os
import re
import asyncio
import urllib.request

from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

# ==================================================
# CONFIGURAZIONE
# ==================================================

TOKEN = os.environ.get("TOKEN")

URL = (
    "https://mappe.protezionecivile.gov.it/"
    "it/mappe-rischi/bollettino-di-criticita/"
)

INTERVALLO = 60
NOME_JOB = "controllo_allerte_sicilia"

# Chat iscritte al monitoraggio (in memoria)
chat_ids = set()

# Ultima firma di allerta inviata a ciascuna chat
ultimi_inviati = {}

# Evita controlli simultanei del sito
controllo_in_corso = False


# ==================================================
# LETTURA E ANALISI DEL BOLLETTINO
# ==================================================

def find_allerta2():
    """Scarica il bollettino ed estrae le allerte della Sicilia."""
    req = urllib.request.Request(
        URL,
        headers={"User-Agent": "Mozilla/5.0"}
    )

    with urllib.request.urlopen(req, timeout=60) as response:
        html = response.read()

    soup = BeautifulSoup(html, "html.parser")

    pannello = soup.find(
        "div",
        {
            "role": "tabpanel",
            "id": "Tabset1Panel1"
        }
    )

    if pannello is None:
        raise ValueError(
            "Pannello del bollettino non trovato. "
            "La struttura del sito potrebbe essere cambiata."
        )

    testo = pannello.get_text(" ", strip=True)
    testo = re.sub(r"\s+", " ", testo)

    if not re.search(r"\bALLERTA\b", testo, re.IGNORECASE):
        raise ValueError(
            "Il testo scaricato non contiene allerte."
        )

    pattern_blocchi = (
        r"((?:ORDINARIA|MODERATA|ELEVATA)"
        r"\s+CRITICIT[AÀ]\s*'?.*?)"
        r"(?=(?:ORDINARIA|MODERATA|ELEVATA)"
        r"\s+CRITICIT[AÀ]|\Z)"
    )

    blocchi = re.findall(
        pattern_blocchi,
        testo,
        re.IGNORECASE
    )

    allerte = []

    for blocco in blocchi:
        # Considera solo i blocchi che menzionano la Sicilia
        if not re.search(r"\bSicilia\s*:", blocco, re.IGNORECASE):
            continue

        if not re.search(
            r"\bNord[-–‑]Orientale\s*,\s*versante\s+ionico\b",
            blocco,
            re.IGNORECASE
        ):
            continue

        rischio_match = re.search(
            r"PER RISCHIO\s+(.+?)\s*/\s*ALLERTA",
            blocco,
            re.IGNORECASE
        )

        # Include GIALLA/GIALLO, ARANCIONE, ROSSA/ROSSO
        livello_match = re.search(
            r"/\s*ALLERTA\s*:?\s*"
            r"(GIALLA|GIALLO|ARANCIONE|ROSSA|ROSSO)\b",
            blocco,
            re.IGNORECASE
        )

        if not rischio_match or not livello_match:
            continue

        rischio = rischio_match.group(1).strip().upper()
        livello = livello_match.group(1).upper()

        if livello == "GIALLA":
            livello = "GIALLO"
        elif livello == "ROSSA":
            livello = "ROSSO"

        risultato = {
            "rischio": rischio,
            "livello": livello
        }

        if risultato not in allerte:
            allerte.append(risultato)

    if not blocchi:
        raise ValueError(
            "Nessun blocco di criticità riconosciuto. "
            "Controlla la struttura del bollettino."
        )

    return allerte


# ==================================================
# CONTROLLO PERIODICO E INVIO TELEGRAM
# ==================================================

async def find_allerta(context: ContextTypes.DEFAULT_TYPE):
    """Controlla il bollettino e gestisce le notifiche con logica di rientro."""
    global controllo_in_corso

    if controllo_in_corso:
        print("Controllo già in corso: salto questa esecuzione.")
        return

    if not chat_ids:
        print("Nessuna chat iscritta al monitoraggio.")
        return

    controllo_in_corso = True

    try:
        allerte = await asyncio.to_thread(find_allerta2)

        allerte.sort(
            key=lambda a: (a["rischio"], a["livello"])
        )

        # Firma dello stato attuale (es. tupla di tuple con rischio e livello)
        firma = tuple(
            (a["rischio"], a["livello"])
            for a in allerte
        )

        for chat_id in list(chat_ids):
            vecchia_firma = ultimi_inviati.get(chat_id)

            # Se lo stato per questa chat non è cambiato, non facciamo nulla
            if vecchia_firma == firma:
                continue

            try:
                messaggio = None

                if allerte:
                    # Caso 1: Ci sono allerte attive
                    righe = [
                        "🚨 BOLLETTINO ALLERTE - SICILIA (Santa Teresa di Riva)",
                        "",
                    ]
                    for allerta in allerte:
                        righe.append(
                            f"• Rischio {allerta['rischio']}: "
                            f"ALLERTA {allerta['livello']}"
                        )
                    messaggio = "\n".join(righe)
                else:
                    # Caso 2: Non ci sono allerte (firma == ())
                    # Invia il messaggio di rientro SOLO SE in questa chat era stata inviata un'allerta in precedenza
                    if vecchia_firma and vecchia_firma != ():
                        messaggio = (
                            "✅ **ALLERTA RIENTRATA**\n\n"
                            "Non risultano più allerte attive per la Sicilia (Santa Teresa di Riva)."
                        )

                # Invia il messaggio se è stato valorizzato
                if messaggio is not None:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=messaggio,
                        parse_mode="Markdown"
                    )
                    print(f"Notifica inviata alla chat {chat_id}.")

                # Aggiorna lo stato registrato per questa chat
                ultimi_inviati[chat_id] = firma

            except Exception as e:
                print(f"Errore invio alla chat {chat_id}: {e}")

    except Exception as e:
        print(f"Errore durante il controllo: {e}")

    finally:
        controllo_in_corso = False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    nuova_iscrizione = chat_id not in chat_ids
    chat_ids.add(chat_id)

    if not context.job_queue.get_jobs_by_name(NOME_JOB):
        context.job_queue.run_repeating(
            find_allerta,
            interval=INTERVALLO,
            first=10,
            name=NOME_JOB
        )

    await update.effective_message.reply_text(
        "✅ Monitoraggio delle allerte attivato.\n\n"
        "Riceverai notifiche tempestive in presenza di allerte (Gialla, Arancione, Rossa) e il relativo avviso di rientro."
    )

    if nuova_iscrizione:
        await find_allerta(context)


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    chat_ids.discard(chat_id)
    ultimi_inviati.pop(chat_id, None)

    await update.effective_message.reply_text(
        "🛑 Monitoraggio delle allerte disattivato."
    )

    if not chat_ids:
        for job in context.job_queue.get_jobs_by_name(NOME_JOB):
            job.schedule_removal()


def main():
    if not TOKEN:
        raise RuntimeError(
            "Token mancante. Imposta la variabile "
            "d'ambiente TOKEN prima dell'avvio."
        )

    application = (
        ApplicationBuilder()
        .token(TOKEN)
        .build()
    )

    if application.job_queue is None:
        raise RuntimeError(
            'JobQueue non disponibile. Installa la libreria con: '
            'python -m pip install "python-telegram-bot[job-queue]"'
        )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stop", stop))

    print("Bot avviato. Premi Ctrl+C per fermarlo.")
    application.run_polling()


if __name__ == "__main__":
    main()
