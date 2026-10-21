# Automation-import-Drive

Servizio che, a intervalli regolari, legge un file Excel condiviso su Google Drive, calcola le **differenze** rispetto all'ultima versione confermata e invia a **Infinity** solo le righe nuove, modificate o eliminate. Ogni esecuzione viene registrata e, in caso di errore, il sistema riprova da solo al ciclo successivo senza perdere dati.

## Architettura

```
Schedule Trigger (n8n, ogni N minuti)
   -> Scarica file da Google Drive
   -> POST /process        (servizio Python: legge l'Excel e calcola le differenze)
   -> IF changed
        -> Invia a Infinity    (retry automatico)
        -> POST /commit/{run_id}  (solo se Infinity ha risposto 2xx)
        -> Log SUCCESS nella Data table "runs" + ping heartbeat
        (se errore -> Log FAILED + notifica Telegram)
Error Workflow (Error Trigger) -> log FAILED + notifica per i guasti imprevisti
```

| Componente | Ruolo |
|---|---|
| **n8n** | Schedulazione, accesso a Drive, chiamate HTTP, retry, storico esecuzioni, alert |
| **processor** (FastAPI + pandas) | Lettura dell'Excel, normalizzazione, calcolo delle differenze, snapshot su SQLite |
| **Data table `runs`** (n8n) | Registro leggibile di ogni esecuzione (esito, righe inviate, errore) |
| **healthchecks.io** (o simile) | Heartbeat esterno: avvisa se n8n o la VM si fermano |

### Come funzionano le differenze (commit in due fasi)

1. `POST /process` confronta il file con l'ultimo snapshot **confermato** e restituisce `upserts` (righe nuove o modificate) e `deletes` (chiavi non più presenti). Lo snapshot **non** viene aggiornato.
2. n8n invia le differenze a Infinity.
3. Solo se Infinity risponde con successo, n8n chiama `POST /commit/{run_id}` e lo snapshot avanza.

Se qualcosa fallisce prima del commit, lo snapshot non cambia: al ciclo successivo le stesse differenze vengono ricalcolate e rinviate. Per questo l'endpoint di Infinity deve essere **idempotente** (upsert sulla chiave); il `run_id` viene inviato anche come header `Idempotency-Key`.

Protezioni integrate nel servizio:

- **File non valido** (colonna chiave assente, chiavi vuote o duplicate): errore 422 prima di inviare qualunque cosa.
- **Cancellazioni anomale**: se le righe eliminate superano `MAX_DELETE_RATIO` (50% per default) dello snapshot, l'elaborazione viene bloccata con errore 409.
- **Righe di totale** (chiave vuota e nessun testo nelle altre colonne): scartate automaticamente.
- **Normalizzazione dei valori** (numeri, spazi, a capo, celle vuote): salvare il file con un'altra applicazione non genera falsi cambiamenti.
- **Lock** interno: due elaborazioni non possono sovrapporsi.

## Struttura del repository

```
.
├── docker-compose.yaml
├── .env.example          # modello delle variabili d'ambiente
├── .gitignore
├── README.md
├── processor/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── app.py
├── workflows/            # export JSON dei workflow n8n (senza credenziali)
├── data/                 # snapshot SQLite (non versionato)
└── logs/                 # log del servizio Python (non versionato)
```

## Requisiti

- Docker Engine e Docker Compose v2
- Un file Excel (`.xlsx`) su Google Drive condiviso con il service account (o accessibile tramite link)
- Un endpoint Infinity che accetti upsert idempotenti
- Per la produzione: una VM (Proxmox/Ubuntu Server) con almeno 2 vCPU, 4 GB di RAM e 30 GB di disco

## Configurazione

Copia il file di esempio e compilalo:

```bash
cp .env.example .env
```

| Variabile | Descrizione |
|---|---|
| `N8N_ENCRYPTION_KEY` | Chiave che cifra le credenziali salvate in n8n. Genera il valore con `openssl rand -hex 32` e **conservalo in un password manager**: senza, un backup non è ripristinabile |
| `VM_IP` | IP o nome host del server, usato per i link e i webhook di n8n |
| `KEY_COLUMN` | Colonna (o colonne separate da virgola) che identifica in modo univoco ogni riga, es. `Product code`. Non usare un numero progressivo di riga |

Altre impostazioni del servizio `processor` (variabili d'ambiente nel compose):

| Variabile | Default | Descrizione |
|---|---|---|
| `SHEET_NAME` | `0` | Indice o nome del foglio da leggere |
| `MAX_DELETE_RATIO` | `0.5` | Soglia di cancellazioni oltre la quale l'elaborazione viene bloccata |

L'intestazione della tabella viene cercata automaticamente nelle prime 30 righe del foglio: non serve che sia sulla prima riga.

## Avvio

```bash
docker compose up -d --build
docker compose ps                      # entrambi i servizi "Up", processor "healthy"
curl http://localhost:8000/health      # {"status":"ok"}
```

n8n è raggiungibile su `http://<VM_IP>:5678`. Al primo accesso crea l'account amministratore.

## Configurazione di n8n

1. **Credenziali Google**: crea un Service Account nella Google Cloud Console (Drive API abilitata), scarica la chiave JSON e condividi l'Excel con l'email del service account in sola lettura. In n8n crea la credenziale *Google Service Account API*.
2. **Data table `runs`**: crea la tabella con le colonne `run_id`, `status`, `upserts`, `deletes`, `error`, `execution_id`.
3. **Importa i workflow** dalla cartella `workflows/` (*Import from file*) e riassegna credenziali e tabella nei nodi.
4. Verifica che il nodo `Calcola differenze` punti a `http://processor:8000/process` e il nodo `Commit` a `http://processor:8000/commit/{run_id}`.
5. Nel workflow principale imposta in *Settings* l'**Error Workflow** e, se vuoi, *Save successful production executions* su *Do not save* per non accumulare esecuzioni inutili.
6. Collega Telegram (o email) per le notifiche e un check su healthchecks.io per l'heartbeat.
7. Imposta il **Schedule Trigger** (consigliato ogni 2-5 minuti) e **attiva** il workflow.

> Le credenziali non vengono incluse nell'export dei workflow: vanno ricreate su ogni istanza di n8n.

## Prima esecuzione (baseline)

Lo snapshot parte vuoto, quindi al primo ciclo **tutte le righe risultano nuove**.

- Se Infinity è vuoto, lascia che il primo ciclo invii tutto.
- Se Infinity contiene già i dati, allinea lo stato senza inviare nulla:

```bash
curl -F "file=@/percorso/esempio.xlsx" http://localhost:8000/process
curl -X POST http://localhost:8000/commit/<RUN_ID_RESTITUITO>
curl http://localhost:8000/state       # snapshot_rows deve essere > 0
```

## API del servizio processor

| Endpoint | Descrizione |
|---|---|
| `GET /health` | Controllo di stato |
| `GET /state` | Righe nello snapshot, hash del file confermato, eventuale run in sospeso |
| `POST /process` | Riceve l'Excel (campo multipart `file`) e restituisce `changed`, `run_id`, `upserts`, `deletes` |
| `POST /commit/{run_id}` | Conferma lo snapshot per quella run |

## Log e monitoraggio

- **Storico esecuzioni**: interfaccia di n8n (scheda *Executions*).
- **Registro delle esecuzioni**: Data table `runs`.
- **Log del servizio Python**: `logs/processor.log` (con `run_id` per correlare gli eventi) e `docker compose logs processor`.
- **Stato dei dati**: `curl http://localhost:8000/state` oppure apertura di `data/state.db` in sola lettura con DB Browser for SQLite.
- **Heartbeat**: avviso esterno se i ping dal workflow smettono di arrivare.

## Operazioni utili

```bash
docker compose logs -f processor              # log in tempo reale
docker compose restart processor              # riavvio del solo servizio Python
docker compose up -d --build                  # applica modifiche al codice
```

Reset dello snapshot (ad esempio dopo aver cambiato `KEY_COLUMN`):

```bash
docker compose stop processor
rm data/state.db
docker compose start processor
```

Dopo il reset rifai la procedura di baseline descritta sopra.

## Problemi comuni

| Sintomo | Causa probabile |
|---|---|
| `EAI_AGAIN processor` in n8n | Il container `processor` è fermo o in crash: controlla `docker compose ps -a` e i log |
| `422 Colonna chiave ... assente` | `KEY_COLUMN` non coincide con l'intestazione dell'Excel (il messaggio elenca le colonne trovate) |
| `422 Chiavi duplicate` | La chiave scelta non è univoca: usa più colonne separate da virgola |
| `409 Troppe cancellazioni` | Il file sembra molto diverso dallo snapshot (file sbagliato o svuotato): verifica prima di intervenire |
| Vengono segnalate tutte le righe a ogni ciclo | Il commit non viene eseguito (`snapshot_rows` resta 0), oppure è cambiata la chiave |
| Le modifiche non vengono rilevate | In n8n è stato rieseguito solo un nodo: esegui l'intero workflow. Controlla anche che `file_hash` cambi dopo il salvataggio |

## Messa in produzione sulla VM

1. Installa Docker (repository ufficiale) e `qemu-guest-agent`; imposta il fuso orario `Europe/Rome`.
2. Clona il repository, crea il `.env` e avvia con `docker compose up -d --build`.
3. Importa i workflow, ricrea credenziali e Data table, esegui il baseline e attiva il workflow.
4. Spegni l'istanza di sviluppo, per non avere due flussi attivi verso Infinity.
5. Riavvia la VM per verificare che tutto riparta da solo.

Backup consigliati: backup della VM da Proxmox (vzdump), cartella `data/`, volume `n8n_data` e export JSON dei workflow nel repository.

## Sicurezza

- Non versionare mai `.env`, la chiave JSON del service account o i file Excel con dati reali (sono già esclusi da `.gitignore`).
- Pubblica la porta `8000` del servizio `processor` solo in locale (`127.0.0.1:8000:8000`) o rimuovila: n8n lo raggiunge già dalla rete interna di Docker.
- Se n8n è raggiungibile da una rete non fidata, mettilo dietro un reverse proxy con HTTPS e rimuovi `N8N_SECURE_COOKIE=false`.
- Fissa la versione dell'immagine di n8n e aggiornala solo dopo uno snapshot della VM.