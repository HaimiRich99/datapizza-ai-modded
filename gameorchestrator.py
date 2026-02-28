import os
import yaml
import asyncio
import json
import aiohttp
from pathlib import Path

# Import dal framework datapizza-ai
from datapizza.clients.openai_like import OpenAILikeClient
from datapizza.agents.agent import Agent
from datapizza.memory import Memory
from datapizza.type import TextBlock, ROLE
from datapizza.tools.mcp_client import MCPClient

class GameOrchestrator:
    def __init__(self, api_key: str, restaurant_id: str):
        self.api_key = api_key
        self.restaurant_id = restaurant_id
        self.base_url = "https://api.hackapizza.com"
        
        # Inizializziamo il Client LLM (Regolo.ai) come richiesto dal regolamento
        self.llm_client = OpenAILikeClient(
            api_key=api_key,
            model="gpt-oss-120b",
            base_url="https://api.regolo.ai/v1",
        )
        
        # La memoria a lungo termine condivisa tra gli agenti
        self.shared_memory = Memory()
        
        self.prompts_dir = Path(__file__).parent / "prompts"
        
        # Inizializziamo la Task Force
        self._init_agents()

    def _load_prompt(self, agent_filename: str) -> str:
        """Legge il system_prompt dal file YAML."""
        file_path = self.prompts_dir / f"{agent_filename}.yaml"
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
                return data.get("system_prompt", "Sei un assistente generico. (ERRORE PROMPT)")
        except FileNotFoundError:
            print(f"🚨 ALLARME: Impossibile trovare il file prompt {file_path}! L'agente sarà muto.")
            return ""

    def _init_agents(self):
        """Istanzia gli agenti, recupera i tool MCP dal server e li assegna."""
        print("🛠️ Inizializzazione brigata galattica in corso...")
        
        # 1. Configurazione del Client MCP
        mcp_url = f"{self.base_url}/mcp" 
        self.mcp_client = MCPClient(
            url=mcp_url,
            headers={"x-api-key": self.api_key} 
        )
        
        # 2. Scarichiamo la definizione di tutti i tool disponibili dal server
        print("📡 Recupero dei tool MCP dal server della Federazione...")
        try:
            all_tools = self.mcp_client.list_tools()
        except Exception as exc:
            # In ambienti senza connettività o chiave errata potremmo ricevere
            # un ExceptionGroup oppure qualsiasi altra eccezione. Non vogliamo
            # far crashare l'inizializzazione del bot per questo motivo;
            # continueremo con una lista vuota di tool e segnaleremo l'errore.
            print(f"⚠️ Impossibile recuperare i tool MCP: {exc}")
            all_tools = []

        
        # Helper function per filtrare i tool in base al nome
        def get_tools_by_names(names: list[str]):
            return [t for t in all_tools if t.name in names]

        # 3. Assegnazione Tattica dei Tool agli Agenti
        pr_prompt = self._load_prompt("pr_agent")
        self.pr_agent = Agent(
            name="pr_agent",
            client=self.llm_client,
            system_prompt=pr_prompt,
            tools=get_tools_by_names(["send_message", "save_menu"])
        )
        
        shark_prompt = self._load_prompt("shark_agent")
        self.shark_agent = Agent(
            name="shark_agent",
            client=self.llm_client,
            system_prompt=shark_prompt,
            tools=get_tools_by_names(["closed_bid"])
        )
        
        chef_prompt = self._load_prompt("chef_agent")
        self.chef_agent = Agent(
            name="chef_agent",
            client=self.llm_client,
            system_prompt=chef_prompt,
            tools=get_tools_by_names(["prepare_dish", "serve_dish", "update_restaurant_is_open"])
        )
        
        print(f"✅ Brigata pronta! Tool assegnati correttamente.")

    # ==========================================
    # CHIAMATE HTTP (SPIONAGGIO E DATI)
    # ==========================================
    
    async def _fetch_data(self, endpoint: str, params: dict | None = None) -> dict:
        """Metodo base per fare chiamate GET al server."""
        url = f"{self.base_url}{endpoint}"
        headers = {"x-api-key": self.api_key}
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, headers=headers, params=params) as response:
                    if response.status == 200:
                        return await response.json()
                    return {}
            except Exception as e:
                print(f"🔥 Errore di rete su {endpoint}: {e}")
                return {}

    async def update_restaurant_inventory(self):
        """Scarica i fondi e l'inventario del nostro ristorante e li salva in memoria."""
        data = await self._fetch_data(f"/restaurant/{self.restaurant_id}")
        if data:
            saldo = data.get("balance", 0)
            inventario = data.get("inventory", [])
            report = f"--- STATO RISTORANTE ---\nSaldo: {saldo}\nInventario: {json.dumps(inventario)}"
            # Salviamo il report nella memoria condivisa come se fosse un messaggio dell'utente/sistema
            self.shared_memory.add_turn(TextBlock(content=report), role=ROLE.USER)

    async def fetch_available_recipes(self):
        """Scarica le ricette disponibili."""
        ricette = await self._fetch_data("/recipes")
        self.available_recipes = ricette.get("recipes", [])

    async def fetch_competitor_bids(self, turn_id: str):
        """Spia le aste degli avversari e salva i dati in memoria."""
        history = await self._fetch_data("/bid_history", params={"turn_id": turn_id})
        if history:
            report = f"--- STORICO ASTE ---\n{json.dumps(history)}"
            self.shared_memory.add_turn(TextBlock(content=report), role=ROLE.USER)

    # ==========================================
    # CICLO EVENTI E ROUTING
    # ==========================================

    async def listen_and_route(self):
        """Il nostro orecchio sempre teso verso il Multiverso."""
        print(f"🎧 Connessione al flusso SSE per il ristorante {self.restaurant_id}...")
        
        headers = {"x-api-key": self.api_key}
        url = f"{self.base_url}/events/{self.restaurant_id}" 
        
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, headers=headers) as response:
                    if response.status != 200:
                        print(f"🚨 ERRORE SERVER {response.status}: Chiave API errata o ristorante esploso.")
                        return

                    async for line in response.content:
                        if not line:
                            continue
                        
                        decoded_line = line.decode('utf-8').strip()
                        if decoded_line.startswith("data:"):
                            data_str = decoded_line[5:].strip()
                            
                            if data_str == "connected":
                                print("🟢 Connessi al server della Federazione! In attesa di eventi...")
                                continue
                                
                            try:
                                event = json.loads(data_str)
                                await self._handle_event(event)
                            except json.JSONDecodeError:
                                print(f"⚠️ Roba non euclidea ricevuta (JSON non valido): {data_str}")
                                continue
            except Exception as e:
                print(f"💥 Connessione interrotta brutalmente: {e}. Tenterò la riconnessione al prossimo ciclo.")

    async def _handle_event(self, event: dict):
        """Il Vigile Urbano. Smista l'evento all'agente di competenza."""
        event_type = event.get("type")
        event_data = event.get("data", {})
        
        if event_type == "heartbeat":
            return
            
        print(f"\n📩 Nuovo Evento SSE Ricevuto: {event_type}")
        
        if event_type == "game_started":
            print("🚀 IL GIOCO È INIZIATO! Reset della memoria e partenza.")
            self.shared_memory.clear() 
            
        elif event_type == "game_phase_changed":
            phase = event_data.get("phase")
            turn_id = event_data.get("turn_id", "")
            print(f"🔄 Cambio Fase! Entriamo in: {phase.upper()}")
            
            context_prompt = f"Il sistema notifica che è appena iniziata la fase {phase}. Fai la tua mossa."
            
            if phase == "speaking":
                # Scarica info fresche e spia i competitor prima di parlare
                await self.fetch_available_recipes()
                if turn_id:
                    await self.fetch_competitor_bids(turn_id)
                
                print("🗣️ Passo il microfono all'Agente PR...")
                asyncio.create_task(self._run_agent_async(self.pr_agent, context_prompt))
                
            elif phase == "closed_bid":
                # FONDAMENTALE: Verifica il saldo prima di scommettere
                await self.update_restaurant_inventory()
                
                print("🦈 Libero lo Squalo. Andiamo a vincere quest'asta...")
                asyncio.create_task(self._run_agent_async(self.shark_agent, context_prompt))
                
            elif phase == "waiting":
                print("⏳ Fase di attesa. Controllo i risultati dell'asta...")
                await self.update_restaurant_inventory()
                
            elif phase == "stopped":
                print("🛑 Turno concluso. Ferma le macchine.")
                
        elif event_type == "client_spawned":
            client_name = event_data.get("clientName", "Alieno Anonimo")
            order = event_data.get("orderText", "")
            print(f"🛎️ NUOVO CLIENTE! Il tavolo 4 ({client_name}) ha ordinato: {order}")
            
            chef_prompt = f"Nuovo cliente al bancone: {client_name}. Ordine esatto: '{order}'. Prepara e servi il piatto corretto."
            asyncio.create_task(self._run_agent_async(self.chef_agent, chef_prompt))

    async def _run_agent_async(self, agent: Agent, prompt: str):
        """
        Esegue l'agente in un thread separato. 
        Passa SEMPRE la shared_memory per garantire la persistenza di contesto.
        """
        print("🧠 L'agente sta elaborando la strategia...")
        try:
            # Passiamo esplicitamente 'memory=self.shared_memory' al metodo run dell'agente
            response = await asyncio.to_thread(agent.run, prompt, memory=self.shared_memory)
            
            # Nota: 'response' di Datapizza potrebbe essere un oggetto complesso (es. Block).
            # Accediamo al suo contenuto se possibile, altrimenti lo stampiamo interamente.
            content = getattr(response, "content", getattr(response, "text", str(response)))
            print(f"✅ Agente ha completato l'operazione. Risposta: {content}")
        except Exception as e:
            print(f"🔥 ERRORE CRITICO: L'agente è andato in kernel panic: {e}")