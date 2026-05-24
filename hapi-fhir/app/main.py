import psycopg2
import requests
from fastapi import FastAPI, HTTPException, Depends
from psycopg2.extras import RealDictCursor
import os
from datetime import datetime, timedelta, timezone
from jose import JWTError, jwt
from passlib.context import CryptContext
from fastapi.security import OAuth2PasswordBearer
import time
import asyncio
import threading
from fastapi import BackgroundTasks

app = FastAPI()

# Configurações 
SECRET_KEY = "admin"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30

# Configurações EHRbase
EHRBASE_URL = "http://ehrbase:8080/ehrbase/rest/openehr/v1"
EHRBASE_URL_ADMIN = "http://ehrbase:8080/ehrbase/rest/openehr/v1" 
EHRBASE_USER = "admin-user"
EHRBASE_PASS = "RequirementPassword"
EHR_AUTH = (EHRBASE_USER, EHRBASE_PASS)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")

FHIR_SERVER_URL = "http://fhir:8080/fhir"

def get_db_connection():
    return psycopg2.connect(
        host="db",  
        port=5432,
        database="tp1",
        user="admin",
        password="admin"
    )

def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password):
    return pwd_context.hash(password)

def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

async def get_current_user(token: str = Depends(oauth2_scheme)):
    credentials_exception = HTTPException(
        status_code=401,
        detail="Não foi possível validar as credenciais",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
        return username
    except JWTError:
        raise credentials_exception

def validar_recurso_fhir(recurso_json, tipo_recurso):
    url_valida = f"{FHIR_SERVER_URL}/{tipo_recurso}/$validate"
    try:
        res = requests.post(url_valida, json=recurso_json, timeout=5)
        resultado = res.json()
        
        for issue in resultado.get('issue', []):
            if issue.get('severity') == 'error':
                return False, issue.get('diagnostics')
        return True, "Válido"
    except Exception as e:
        return False, f"Servidor de validação incontactável: {str(e)}"

def get_or_create_ehr(numero_utente, patient_fhir_id):
    # Namespace com hífens — único formato aceite pelo regex do EHRbase
    NAMESPACE = "pt-sns-utente"

    search_url_sns  = f"{EHRBASE_URL}/ehr?subject_id={numero_utente}&subject_namespace={NAMESPACE}"
    search_url_fhir = f"{EHRBASE_URL}/ehr?subject_id={patient_fhir_id}&subject_namespace={NAMESPACE}"

    # Procurar pelo N.º de Utente
    res = requests.get(search_url_sns)
    if res.status_code == 200:
        return res.json()['ehr_id']['value']

    # Procurar pelo ID do FHIR
    res_fhir = requests.get(search_url_fhir)
    if res_fhir.status_code == 200:
        return res_fhir.json()['ehr_id']['value']

    # Não existe — criar o EHR
    payload = {
        "_type": "EHR_STATUS",
        "archetype_node_id": "openEHR-EHR-EHR_STATUS.generic.v1",
        "name": {
            "_type": "DV_TEXT",
            "value": "EHR Status"
        },
        "is_queryable": True,
        "is_modifiable": True,
        "subject": {
            "_type": "PARTY_SELF",
            "external_ref": {
                "_type": "PARTY_REF",
                "id": {
                    "_type": "GENERIC_ID",
                    "value": str(numero_utente),
                    "scheme": "SNS"
                },
                "namespace": NAMESPACE,
                "type": "PERSON"
            }
        }
    }

    create_res = requests.post(f"{EHRBASE_URL}/ehr", json=payload)

    if create_res.status_code in [200, 201]:
        if 'Location' in create_res.headers:
            return create_res.headers['Location'].split('/')[-1]
        elif 'ETag' in create_res.headers:
            return create_res.headers['ETag'].replace('"', '')
        return create_res.json()['ehr_id']['value']

    if create_res.status_code == 409:
        print(f"⚠️ [EHR] Conflito 409 — recuperando EHR existente...")
        for url in [search_url_sns, search_url_fhir]:
            retry = requests.get(url)
            if retry.status_code == 200:
                return retry.json()['ehr_id']['value']

    print(f"❌ Erro crítico do EHRbase: {create_res.text}")
    raise Exception(f"EHRbase devolveu status {create_res.status_code}: {create_res.text}")

def upload_template():
    """Tenta fazer o upload do template .opt para o EHRbase usando caminhos absolutos"""
    url = f"{EHRBASE_URL}/definition/template/adl1.4"
    
    base_dir = os.path.dirname(os.path.abspath(__file__))
    templates_dir = os.path.join(base_dir, "templates")
    template_path = os.path.join(templates_dir, "sinais_vitais.opt")
    
    print(f"📁 A API está a procurar o template em: {template_path}")
    
    if not os.path.exists(template_path):
        print(f"❌ Erro: O ficheiro {template_path} não existe fisicamente.")
        try:
            arquivos_encontrados = os.listdir(templates_dir)
            print(f"🔍 Ficheiros que realmente existem dentro de {templates_dir}: {arquivos_encontrados}")
        except Exception as e:
            print(f"⚠️ Não foi possível listar a pasta de templates: {e}")
        return

    for i in range(30): 
        try:
            with open(template_path, "rb") as f:
                headers = {'Content-Type': 'application/xml'}
                res = requests.post(url, data=f, auth=None, headers=headers)
                if res.status_code in [200, 201, 409]:
                    print("✅ Passo 1: Template openEHR carregado com sucesso!")
                    return
                else:
                    print(f"⚠️ Aguardando EHRbase... Status: {res.status_code} (Tentativa {i+1}/30)")
        except Exception as e:
            print(f"⚠️ EHRbase ainda não responde... ({str(e)}) (Tentativa {i+1}/30)")
        time.sleep(5)

# --- MERGE: Mapeamento baseado no requisito 4.1 do enunciado ---
MAPA_SINAIS_VITAIS = {
    # blood_pressure.v2: at0006 é EVENT genérico → POINT_EVENT aceite
    "8480-6": {
        "nome": "Systolic",
        "archetype": "openEHR-EHR-OBSERVATION.blood_pressure.v2",
        "history_node": "at0001",
        "event_node": "at0006",
        "event_type": "POINT_EVENT",
        "data_node": "at0003",
        "item_node": "at0004",
        "unidade": "mm[Hg]",
    },
    "8462-4": {
        "nome": "Diastolic",
        "archetype": "openEHR-EHR-OBSERVATION.blood_pressure.v2",
        "history_node": "at0001",
        "event_node": "at0006",
        "event_type": "POINT_EVENT",
        "data_node": "at0003",
        "item_node": "at0005",
        "unidade": "mm[Hg]",
    },
    # pulse.v2: at0003 é INTERVAL_EVENT OBRIGATÓRIO no .opt (width + math_function obrigatórios)
    "8867-4": {
        "nome": "Rate",
        "archetype": "openEHR-EHR-OBSERVATION.pulse.v2",
        "history_node": "at0002",
        "event_node": "at0003",
        "event_type": "POINT_EVENT",
        "data_node": "at0001",
        "item_node": "at0004",
        "unidade": "/min",
    },
    # body_temperature.v2: at0003 é EVENT genérico → POINT_EVENT aceite
    "8310-5": {
        "nome": "Temperature",
        "archetype": "openEHR-EHR-OBSERVATION.body_temperature.v2",
        "history_node": "at0002",
        "event_node": "at0003",
        "event_type": "POINT_EVENT",
        "data_node": "at0001",
        "item_node": "at0004",
        "unidade": "Cel",
    },
    # pulse_oximetry.v1: at0002 é EVENT genérico → POINT_EVENT aceite
    "59408-5": {
        "nome": "SpO2",
        "archetype": "openEHR-EHR-OBSERVATION.pulse_oximetry.v1",
        "history_node": "at0001",
        "event_node": "at0002",
        "event_type": "POINT_EVENT",
        "data_node": "at0003",
        "item_node": "at0006",
        "unidade": "%",
    },
    # body_weight.v2: at0003 é EVENT genérico → POINT_EVENT aceite
    "29463-7": {
        "nome": "Weight",
        "archetype": "openEHR-EHR-OBSERVATION.body_weight.v2",
        "history_node": "at0002",
        "event_node": "at0003",
        "event_type": "POINT_EVENT",
        "data_node": "at0001",
        "item_node": "at0004",
        "unidade": "kg",
    },
    # respiration.v2: at0002 é EVENT genérico → POINT_EVENT aceite
    "9279-1": {
        "nome": "Rate",
        "archetype": "openEHR-EHR-OBSERVATION.respiration.v2",
        "history_node": "at0001",
        "event_node": "at0002",
        "event_type": "POINT_EVENT",
        "data_node": "at0003",
        "item_node": "at0004",
        "unidade": "/min",
    },
}

def build_openehr_composition(fhir_payload: dict, nome_medico: str, fhir_medico_id: str) -> dict:
    try:
        valor_medicao = fhir_payload['valueQuantity']['value']
        data_execucao = fhir_payload['effectiveDateTime']

        # Determinar o LOINC code
        loinc_code = None
        for coding in fhir_payload.get('code', {}).get('coding', []):
            if coding.get('system') == 'http://loinc.org' or coding.get('system') == 'loinc':
                loinc_code = coding.get('code')
                break

        if not loinc_code and fhir_payload.get('code', {}).get('coding'):
            loinc_code = fhir_payload['code']['coding'][0].get('code')

        if loinc_code not in MAPA_SINAIS_VITAIS:
            print(f"⚠️ LOINC {loinc_code} não suportado no mapa openEHR")
            return None

        info = MAPA_SINAIS_VITAIS[loinc_code]
        unidade = fhir_payload['valueQuantity'].get('code', info.get('unidade', '1'))

        # Correção do formato ISO Datetime para aceitar a biblioteca Java do EHRbase
        if data_execucao and data_execucao.endswith('Z'):
            data_execucao = data_execucao.replace('Z', '')

        # Definição do nó de valor dinâmico conforme o tipo exigido pelo Template (Proportion vs Quantity)
        if loinc_code == "59408-5":
            # Saturação exige DV_PROPORTION (Mapeia o Numerator) para passar no validador
            value_block = {
                "_type": "DV_PROPORTION",
                "numerator": float(valor_medicao),
                "denominator": 100.0,
                "type": 3
            }
        else:
            # Restantes medições utilizam o clássico DV_QUANTITY
            value_block = {
                "_type": "DV_QUANTITY",
                "magnitude": float(valor_medicao),
                "units": unidade
            }

        composer_block = {
            "_type": "PARTY_IDENTIFIED",
            "name": nome_medico,
            "external_ref": {
                "_type": "PARTY_REF",
                "id": {
                    "_type": "GENERIC_ID",
                    "value": str(fhir_medico_id),  # Guarda o ID/Cédula do Practitioner FHIR
                    "scheme": "fhir"
                },
                "namespace": "pt-cedula-profissional",
                "type": "ORGANISATION"
            }
        }

        composition = {
            "_type": "COMPOSITION",
            "archetype_node_id": "openEHR-EHR-COMPOSITION.encounter.v1",
            "name": {"_type": "DV_TEXT", "value": "Encounter"},
            "archetype_details": {
                "_type": "ARCHETYPED",
                "archetype_id": {
                    "_type": "ARCHETYPE_ID",
                    "value": "openEHR-EHR-COMPOSITION.encounter.v1"
                },
                "template_id": {"_type": "TEMPLATE_ID", "value": "sinais_vitais"},
                "rm_version": "1.0.4"
            },
            "language": {
                "_type": "CODE_PHRASE",
                "terminology_id": {"_type": "TERMINOLOGY_ID", "value": "ISO_639-1"},
                "code_string": "en"   # O template original possui language "en"
            },
            "territory": {
                "_type": "CODE_PHRASE",
                "terminology_id": {"_type": "TERMINOLOGY_ID", "value": "ISO_3166-1"},
                "code_string": "PT"
            },
            "category": {
                "_type": "DV_CODED_TEXT",
                "value": "event",
                "defining_code": {
                    "_type": "CODE_PHRASE",
                    "terminology_id": {"_type": "TERMINOLOGY_ID", "value": "openehr"},
                    "code_string": "433"
                }
            },
            "composer": composer_block,
            "context": {
                "_type": "EVENT_CONTEXT",
                "start_time": {"_type": "DV_DATE_TIME", "value": data_execucao},
                "setting": {
                    "_type": "DV_CODED_TEXT",
                    "value": "secondary medical care",
                    "defining_code": {
                        "_type": "CODE_PHRASE",
                        "terminology_id": {"_type": "TERMINOLOGY_ID", "value": "openehr"},
                        "code_string": "232"
                    }
                }
            },
            "content": [
                {
                    "_type": "OBSERVATION",
                    "archetype_node_id": info["archetype"],
                    "name": {"_type": "DV_TEXT", "value": info["nome"]},
                    "archetype_details": {
                        "_type": "ARCHETYPED",
                        "archetype_id": {
                            "_type": "ARCHETYPE_ID",
                            "value": info["archetype"]
                        },
                        "rm_version": "1.0.4"
                    },
                    "language": {
                        "_type": "CODE_PHRASE",
                        "terminology_id": {"_type": "TERMINOLOGY_ID", "value": "ISO_639-1"},
                        "code_string": "en"
                    },
                    "encoding": {
                        "_type": "CODE_PHRASE",
                        "terminology_id": {"_type": "TERMINOLOGY_ID", "value": "IANA_character-sets"},
                        "code_string": "UTF-8"
                    },
                    "subject": {"_type": "PARTY_SELF"},
                    "data": {
                        "_type": "HISTORY",
                        "archetype_node_id": info["history_node"],  
                        "name": {"_type": "DV_TEXT", "value": "History"},
                        "origin": {"_type": "DV_DATE_TIME", "value": data_execucao},
                        "events": [
                            {
                                "_type": info.get("event_type", "POINT_EVENT"),
                                "archetype_node_id": info["event_node"],
                                "name": {"_type": "DV_TEXT", "value": "Any event"},
                                "time": {"_type": "DV_DATE_TIME", "value": data_execucao},
                                # INTERVAL_EVENT (pulse.v2) requer width e math_function obrigatórios
                                **({
                                    "width": {"_type": "DV_DURATION", "value": "PT0S"},
                                    "math_function": {
                                        "_type": "DV_CODED_TEXT",
                                        "value": "actual",
                                        "defining_code": {
                                            "_type": "CODE_PHRASE",
                                            "terminology_id": {"_type": "TERMINOLOGY_ID", "value": "openehr"},
                                            "code_string": "640"
                                        }
                                    }
                                } if info.get("event_type") == "INTERVAL_EVENT" else {}),
                                "data": {
                                    "_type": "ITEM_TREE",
                                    "archetype_node_id": info["data_node"],
                                    "name": {"_type": "DV_TEXT", "value": "Tree"},
                                    "items": [
                                        {
                                            "_type": "ELEMENT",
                                            "archetype_node_id": info["item_node"],
                                            "name": {"_type": "DV_TEXT", "value": info["nome"]},
                                            "value": value_block
                                        }
                                    ]
                                }
                            }
                        ]
                    }
                }
            ]
        }
        return composition
    except Exception as e:
        print(f"❌ Erro crítico ao construir composição openEHR: {e}")
        return None

def _get_hapi_server_time() -> datetime:  # noqa
    """
    Obtém o timestamp actual directamente do HAPI FHIR via cabeçalho Date da resposta.
    Garante que o cursor de polling usa sempre o relógio do servidor HAPI,
    eliminando desfasamentos entre o relógio do container Docker e o da máquina host.
    Aplica uma margem de segurança de 5 segundos para trás para cobrir latências de rede.
    """
    try:
        r = requests.get(f"{FHIR_SERVER_URL}/metadata", timeout=5)
        date_header = r.headers.get("Date")
        if date_header:
            from email.utils import parsedate_to_datetime
            server_time = parsedate_to_datetime(date_header)
            return server_time - timedelta(seconds=5)
    except Exception as e:
        print(f"⚠️ [Polling] Não foi possível obter hora do HAPI — a usar hora local: {e}")
    return datetime.now(timezone.utc) - timedelta(seconds=5)


def fhir_polling_worker():
    """
    Polling periódico em thread dedicado — evita bloquear o event loop do asyncio.
    Usa requests síncronos sem interferir com os endpoints FastAPI.
    """
    print("⏳ [Polling Worker] Inicializado e em conformidade estrita com o TP02...")

    # Aguardar HAPI FHIR ficar online
    while True:
        try:
            r = requests.get(f"{FHIR_SERVER_URL}/metadata", timeout=3)
            if r.status_code == 200:
                print("✅ [Polling Worker] HAPI FHIR online — a iniciar polling.")
                break
        except Exception:
            pass
        print("⏳ [Polling Worker] Aguardando HAPI FHIR...")
        time.sleep(10)

    ultima_verificacao = _get_hapi_server_time()
    print(f"🕐 [Polling] Checkpoint inicial (hora do HAPI): {ultima_verificacao.isoformat()}")

    while True:
        time.sleep(15)  # thread blocking — não afecta o event loop
        try:
            iso_time = ultima_verificacao.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
            print(f"🔍 [Polling] A verificar desde: {iso_time}")

            url_polling = f"{FHIR_SERVER_URL}/Observation?_lastUpdated=gt{iso_time}"
            headers = {"Accept": "application/fhir+json"}
            res = requests.get(url_polling, headers=headers, timeout=10)

            novo_checkpoint = _get_hapi_server_time()

            if res.status_code == 200:
                bundle = res.json()
                entries = bundle.get("entry", [])

                if entries:
                    print(f"🔔 [Polling] Encontradas {len(entries)} novas Observations no HAPI FHIR!")

                    for entry in entries:
                        resource = entry.get("resource", {})
                        fhir_obs_id = resource.get("id")

                        subject = resource.get("subject", {})
                        subject_ref = subject.get("reference", "")
                        fhir_patient_id = subject_ref.split("/")[-1] if "/" in subject_ref else "Desconhecido"

                        utente_sns = None
                        subject_identifier = subject.get("identifier", {})
                        if subject_identifier.get("system") == "https://www.sns.gov.pt/utente":
                            utente_sns = subject_identifier.get("value")

                        if not utente_sns:
                            print(f"⚠️ Observation {fhir_obs_id} ignorada: Não tem N.º Utente do SNS no subject.identifier.")
                            continue

                        if fhir_patient_id == "Desconhecido" and utente_sns:
                            res_pt = requests.get(
                                f"{FHIR_SERVER_URL}/Patient?identifier=https://www.sns.gov.pt/utente|{utente_sns}",
                                headers=headers, timeout=5
                            )
                            if res_pt.status_code == 200:
                                entries_pt = res_pt.json().get("entry", [])
                                if entries_pt:
                                    fhir_patient_id = entries_pt[0]["resource"]["id"]

                        try:
                            ehr_id = get_or_create_ehr(str(utente_sns), str(fhir_patient_id))
                        except Exception as ehr_err:
                            print(f"❌ Erro na Gestão do EHR para o utente {utente_sns}: {ehr_err}")
                            continue

                        performers = resource.get("performer", [])
                        nome_medico = "Sistema Automático"
                        cedula_profissional = "Desconhecido"

                        if performers:
                            performer = performers[0]
                            performer_ref = performer.get("reference")
                            res_practitioner = requests.get(f"{FHIR_SERVER_URL}/{performer_ref}", headers=headers, timeout=5)

                            if res_practitioner.status_code == 200:
                                practitioner_data = res_practitioner.json()
                                names = practitioner_data.get("name", [])
                                if names:
                                    nome_medico = names[0].get("text", "Médico Desconhecido")

                                for p_ident in practitioner_data.get("identifier", []):
                                    if p_ident.get("system") in ["https://www.ordemdosmedicos.pt", "https://www.ordemenfermeiros.pt"]:
                                        cedula_profissional = p_ident.get("value")
                                        break

                                print(f"👤 [Practitioner] Profissional identificado: {nome_medico} | Cédula: {cedula_profissional} → será registado como PARTY_IDENTIFIED na composição")

                        composition = build_openehr_composition(resource, nome_medico, str(cedula_profissional))

                        if composition:
                            comp_url = f"{EHRBASE_URL}/ehr/{ehr_id}/composition?templateId=sinais_vitais"
                            comp_headers = {
                                "Content-Type": "application/json",
                                "Accept": "application/json",
                                "Prefer": "return=representation"
                            }
                            res_ehr = requests.post(comp_url, json=composition, auth=None, headers=comp_headers, timeout=10)
                            if res_ehr.status_code in [200, 201]:
                                print(f"✅ [Polling] Composição gravada no EHRbase! UID: {res_ehr.json().get('uid', {}).get('value')}")
                            else:
                                print(f"❌ [Polling] EHRbase rejeitou a composição: {res_ehr.text}")

            ultima_verificacao = novo_checkpoint

        except Exception as err:
            print(f"⚠️ [Polling Worker] Ocorreu uma falha no ciclo: {err}")

@app.on_event("startup")
async def startup_event():
    print("--- Inicializando Middleware e Templates ---")
    upload_template()
    
    print("--- Verificando Servidor FHIR ---")
    try:
        res = requests.get(f"{FHIR_SERVER_URL}/metadata", timeout=3)
        if res.status_code == 200:
            print("HAPI FHIR: Online e pronto.")
    except Exception:
        print("HAPI FHIR: Servidor offline ou a iniciar.")

    t = threading.Thread(target=fhir_polling_worker, daemon=True)
    t.start()

@app.post("/Register")
async def register(data: dict):
    username = data.get("username")
    password = data.get("password")
    hashed_pw = pwd_context.hash(password) 

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO usuarios (username, password_hash) VALUES (%s, %s)",
            (username, hashed_pw)
        )
        conn.commit()
        return {"msg": "Utilizador criado! Agora já podes fazer login."}
    except Exception as e:
        conn.rollback()
        return {"erro": str(e)}
    finally:
        conn.close()

@app.post("/Login")
async def login(data: dict):
    username = data.get("username")
    password = data.get("password")

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM usuarios WHERE username = %s", (username,))
    user = cur.fetchone()
    cur.close()
    conn.close()

    if not user:
        raise HTTPException(status_code=401, detail="Utilizador não encontrado")
    if not verify_password(password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Password incorreta")

    access_token = create_access_token(data={"sub": user["username"]})
    return {"access_token": access_token, "token_type": "bearer"}

@app.post("/Patient")
async def create_patient(data: dict, current_user: str = Depends(get_current_user)):
    conn = None
    try:
        nome_paciente = data.get('nome', 'Sem Nome')
        genero_raw = data.get('genero', 'unknown')
        numero_utente = data.get('numero_utente', '')
        fhir_telecoms = [
            {"system": "phone" if t.get('tipo') == "telemóvel" else "email", "value": t.get('valor')}
            for t in data.get('telecom', [])
        ]

        fhir_contacts = []
        for con in data.get('contacto', []):
            con_telecoms = [
                {"system": "phone" if tc.get('tipo') == "telemóvel" else "email", "value": tc.get('valor')}
                for tc in con.get('telecom', [])
            ]
            addr = con.get('endereco')
            con_addresses = {"use": "home" if addr.get('tipo') == "casa" else "work", "line": [addr.get('valor')]}
            fhir_contacts.append({
                "relationship": [{"text": "Emergency Contact"}],
                "name": {"family": con.get('nome')},
                "telecom": con_telecoms,
                "address": con_addresses
            })

        fhir_payload = {
            "resourceType": "Patient",
            "active": True,
            "name": [{"text": nome_paciente}],
            "gender": "male" if genero_raw == "m" else "female" if genero_raw == "f" else "unknown",
            "telecom": fhir_telecoms,
            "contact": fhir_contacts,
            "identifier": [
                {
                    "system": "https://www.sns.gov.pt/utente",
                    "value": numero_utente
                }
            ]
        }

        valido, mensagem = validar_recurso_fhir(fhir_payload, "Patient")
        if not valido:
            raise HTTPException(status_code=400, detail=f"Erro de Schema FHIR: {mensagem}")

        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute(
            "INSERT INTO patients (nome, genero, numero_utente) VALUES (%s, %s, %s) RETURNING id",
            (nome_paciente, genero_raw, numero_utente)
        )
        paciente_id_local = cur.fetchone()['id']

        for tel in data.get('telecom', []):
            cur.execute(
                "INSERT INTO telecom (paciente_id, tipo, valor) VALUES (%s, %s, %s)",
                (paciente_id_local, tel.get('tipo'), tel.get('valor'))
            )

        for con in data.get('contacto', []):
            cur.execute(
                "INSERT INTO contacto (paciente_id, nome) VALUES (%s, %s) RETURNING id",
                (paciente_id_local, con.get('nome'))
            )
            contacto_id = cur.fetchone()['id']

            for tel_con in con.get('telecom', []):
                cur.execute(
                    "INSERT INTO telecom (contacto_id, tipo, valor) VALUES (%s, %s, %s)",
                    (contacto_id, tel_con.get('tipo'), tel_con.get('valor'))
                )
            
            end_obj = con.get('endereco')
            if end_obj and isinstance(end_obj, dict):
                cur.execute(
                    "INSERT INTO endereco (contacto_id, tipo, valor) VALUES (%s, %s, %s)",
                    (contacto_id, end_obj.get('tipo'), end_obj.get('valor'))
                )

        conn.commit()

        hapi_url = f"{FHIR_SERVER_URL}/Patient"
        headers = {"Content-Type": "application/fhir+json;charset=utf-8"}
        ehrbase_status = "Pendente (HAPI Falhou)"
        ehr_id_gerado = None

        try:
            hapi_res = requests.post(hapi_url, json=fhir_payload, headers=headers, timeout=10)
            if hapi_res.status_code in [200, 201]:
                fhir_id_gerado = hapi_res.json().get('id')
                cur.execute("UPDATE patients SET fhir_id = %s WHERE id = %s", (str(fhir_id_gerado), paciente_id_local))
                conn.commit()
                
                try:
                    ehr_id_gerado = get_or_create_ehr(str(numero_utente), str(fhir_id_gerado))
                    ehrbase_status = "Sincronizado"
                except Exception as ehr_err:
                    ehrbase_status = f"Erro ao criar no openEHR: {str(ehr_err)}"
                
                return {
                    "status": "sucesso",
                    "id_local": paciente_id_local,
                    "id_fhir": fhir_id_gerado,
                    "sincronizacao_openehr": {
                        "status": ehrbase_status,
                        "ehr_id": ehr_id_gerado
                    }
                }
            else:
                return {
                    "status": "aviso",
                    "id_local": paciente_id_local,
                    "msg": "Gravado localmente, mas rejeitado pelo HAPI final.",
                    "erro": hapi_res.text[:200],
                    "sincronizacao_openehr": {"status": ehrbase_status}
                }
        except Exception as hapi_err:
            return {
                "status": "aviso", 
                "id_local": paciente_id_local, 
                "msg": "SQL OK, HAPI offline.", 
                "erro": str(hapi_err),
                "sincronizacao_openehr": {"status": "Pendente (HAPI Offline)"}
            }

    except Exception as e:
        if conn: conn.rollback()
        if isinstance(e, HTTPException): raise e
        raise HTTPException(status_code=500, detail=f"Erro no processamento: {str(e)}")
    finally:
        if conn: 
            cur.close()
            conn.close()

# --- MERGE: Rota /Observation ajustada de volta para o endpoint canónico do RM ---
@app.post("/Observation")
async def create_observation(data: dict, current_user: str = Depends(get_current_user)):
    print("ALERTA: O pedido da Observation chegou ao meu código Python!")
    conn = None
    try:
        obj_codigo = data.get('codigo', {})
        m = data.get('medicao', {})
        
        # Mapeamos os códigos FHIR primeiro para usar na validação epayload
        lista_codigos_fhir = [
            {
                "system": c.get('system'),
                "code": str(c.get('cod')),
                "display": c.get('disp')
            } for c in obj_codigo.get('coding', [])
        ]

        refer_string = data.get('refer', '')
        local_patient_id = int(refer_string.split('/')[-1]) if '/' in refer_string else None

        if not local_patient_id:
            raise HTTPException(status_code=400, detail="Referência de paciente inválida. Use 'Patient/ID'")

        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute("SELECT fhir_id, nome, numero_utente FROM patients WHERE id = %s", (local_patient_id,))
        paciente_row = cur.fetchone()

        if not paciente_row or not paciente_row['fhir_id']:
            raise HTTPException(
                status_code=400, 
                detail=f"O paciente local {local_patient_id} não está sincronizado com o HAPI."
            )

        fhir_patient_id = paciente_row['fhir_id']

        performer_string = data.get('performer', '')
        local_medico_id = int(performer_string.split('/')[-1]) if '/' in performer_string else None
        
        fhir_medico_id = "Desconhecido"
        nome_medico = current_user  # Fallback caso não venha nenhum no JSON
        
        if local_medico_id:
            cur.execute("SELECT fhir_id, nome FROM medicos WHERE id = %s", (local_medico_id,))
            medico_row = cur.fetchone()
            if medico_row and medico_row['fhir_id']:
                fhir_medico_id = medico_row['fhir_id']
                nome_medico = medico_row['nome']

        fhir_payload = {
            "resourceType": "Observation",
            "status": data.get('estado'),
            "subject": {
                "reference": f"Patient/{fhir_patient_id}",
                "identifier": {
                    "system": "https://www.sns.gov.pt/utente", # O URL exigido pelo enunciado
                    "value": str(paciente_row['numero_utente'] or local_patient_id) # Para efeitos de teste, usamos o ID local como N.º SNS
                }
            },
                "effectiveDateTime": data.get('dataExecucao'),
                "code": {
                    "coding": lista_codigos_fhir,
                    "text": obj_codigo.get('text')
                },
            "valueQuantity": {
                "value": m.get('valor'),
                "unit": m.get('unidade'),
                "system": m.get('sistema'),
                "code": str(m.get('cod'))
            }
        }

        # Só adiciona a propriedade 'performer' se o Array NÃO estiver vazio
        if local_medico_id:
            fhir_payload["performer"] = [
                {
                    "reference": f"Practitioner/{fhir_medico_id}",
                    "display": nome_medico
                }
            ]

        # Validamos o recurso FHIR com o payload corrigido
        valido, mensagem = validar_recurso_fhir(fhir_payload, "Observation")
        if not valido:
            raise HTTPException(status_code=400, detail=f"Erro de Schema FHIR: {mensagem}")
        
        # Persistência relacional SQL Local
        cur.execute(
            """INSERT INTO observacoes (paciente_id, estado, refer, dataExecucao) 
               VALUES (%s, %s, %s, %s) RETURNING id""",
            (local_patient_id, data.get('estado'), refer_string, data.get('dataExecucao'))
        )
        obs_id = cur.fetchone()['id']

        cur.execute(
            "INSERT INTO codigo (observacoes_id, text) VALUES (%s, %s) RETURNING id",
            (obs_id, obj_codigo.get('text'))
        )
        codigo_id = cur.fetchone()['id']

        for item in obj_codigo.get('coding', []):
            cur.execute(
                """INSERT INTO coding (codigo_id, system, cod, disp) 
                   VALUES (%s, %s, %s, %s)""",
                (codigo_id, item.get('system'), item.get('cod'), item.get('disp'))
            )

        cur.execute(
            """INSERT INTO medicao (observacoes_id, valor, unidade, sistema, cod) 
               VALUES (%s, %s, %s, %s, %s)""",
            (obs_id, m.get('valor'), m.get('unidade'), m.get('sistema'), m.get('cod'))
        )
        conn.commit()

        # Envio paralelo para o Servidor HAPI FHIR
        hapi_url = f"{FHIR_SERVER_URL}/Observation"
        headers_fhir = {"Content-Type": "application/fhir+json;charset=utf-8"}
        fhir_obs_id = None

        try:
            hapi_res = requests.post(hapi_url, json=fhir_payload, headers=headers_fhir, timeout=5)
            if hapi_res.status_code in [200, 201]:
                fhir_obs_id = hapi_res.json().get('id')
                cur.execute("UPDATE observacoes SET fhir_id = %s WHERE id = %s", (str(fhir_obs_id), obs_id))
                conn.commit()
            else:
                print(f"⚠️ HAPI rejeitou a submissão: {hapi_res.text}")
        except Exception as hapi_err:
            print(f"⚠️ Servidor HAPI FHIR offline: {hapi_err}")

        # ---------------------------------------------------------------------
        # 5. Integração Automática com o openEHR (EHRbase RM canónico)
        # ---------------------------------------------------------------------
        '''utente_sns = fhir_payload.get('subject', {}).get('identifier', {}).get('value') 
        
        if not utente_sns:
            # Fallback de segurança caso o objeto identifier falhe por algum motivo
            utente_sns = str(local_patient_id)
            
        ehrbase_sync_status = "Não Sincronizado"
        comp_uid = None

        try:
            ehr_id = get_or_create_ehr(utente_sns, fhir_patient_id)
            composition = build_openehr_composition(fhir_payload, nome_medico, str(fhir_medico_id))
            
            if composition:
                # Restaurada a URL canónica do Modelo de Referência (RM) exigida pelo teu dicionário complexo
                comp_url = f"{EHRBASE_URL}/ehr/{ehr_id}/composition?templateId=sinais_vitais"
                comp_headers = {
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Prefer": "return=representation"
                }
                
                res_ehr = requests.post(comp_url, json=composition, auth=None, headers=comp_headers, timeout=5)
                
                if res_ehr.status_code in [200, 201]:
                    ehrbase_sync_status = "Sucesso"
                    comp_uid = res_ehr.json().get('uid', {}).get('value')
                else:
                    ehrbase_sync_status = f"Erro EHRbase ({res_ehr.status_code})"
                    print(f"❌ TEXTO DE REJEIÇÃO DO EHRBASE ({res_ehr.status_code}): {res_ehr.text}")
                    raise HTTPException(
                        status_code=res_ehr.status_code, 
                        detail=f"EHRbase rejeitou a composição: {res_ehr.text}"
                    )
            else:
                raise HTTPException(
                    status_code=400, 
                    detail="A tradução para openEHR falhou. Verifica se o LOINC enviado está mapeado no MAPA_SINAIS_VITAIS."
                )
                
        except Exception as ehr_err:
            if isinstance(ehr_err, HTTPException): 
                raise ehr_err
            raise HTTPException(
                status_code=500, 
                detail=f"Erro interno no bloco openEHR: {str(ehr_err)}"
            )'''

# ---------------------------------------------------------------------
        # 6. Resposta Unificada
        # ---------------------------------------------------------------------
        return {
            "status": "sucesso",
            "id_local_sql": obs_id,
            "id_fhir_hapi": fhir_obs_id if fhir_obs_id else "Falhou/Offline",
            "msg": "Guardado no FHIR. O Polling enviará para o EHRbase em breve."
        }

    except Exception as e:
        if conn: conn.rollback()
        if isinstance(e, HTTPException): raise e
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            cur.close()
            conn.close()

@app.post("/Practitioner")
async def create_practitioner(data: dict, current_user: str = Depends(get_current_user)):
    conn = None
    try:
        nome_medico = data.get('nome', 'Médico Desconhecido')
        genero_raw = data.get('genero', 'unknown')
        especialidade = data.get('especialidade', 'Clínica Geral')
        cedula = data.get('cedula', '')

        fhir_telecoms = []
        fhir_addresses = []
        contactos_input = data.get('contacto', [])

        if contactos_input:
            primeiro_con = contactos_input[0]
            fhir_telecoms = [
                {"system": "phone" if tc.get('tipo') == "telemóvel" else "email", "value": tc.get('valor')}
                for tc in primeiro_con.get('telecom', [])
            ]
            addr_in = primeiro_con.get('endereco')
            if addr_in and isinstance(addr_in, dict):
                fhir_addresses = [{
                    "use": "work" if addr_in.get('tipo') == "trabalho" else "home",
                    "line": [addr_in.get('valor')]
                }]

        fhir_payload = {
            "resourceType": "Practitioner",
            "active": True,
            "name": [{"text": nome_medico}],
            "gender": "male" if genero_raw == "m" else "female" if genero_raw == "f" else "unknown",
            "telecom": fhir_telecoms,
            "address": fhir_addresses,
            "qualification": [{"code": {"text": especialidade}}],
            "identifier": [
                {
                    "system": "https://www.ordemdosmedicos.pt",
                    "value": cedula
                }
            ]
        }

        valido, mensagem = validar_recurso_fhir(fhir_payload, "Practitioner")
        if not valido:
            raise HTTPException(status_code=400, detail=f"Erro no Schema Practitioner: {mensagem}")

        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        cur.execute(
            "INSERT INTO medicos (nome, genero, especialidade) VALUES (%s, %s, %s) RETURNING id",
            (nome_medico, genero_raw, especialidade)
        )
        medico_id_local = cur.fetchone()['id']

        for con in contactos_input:
            cur.execute(
                "INSERT INTO contacto (medico_id, nome) VALUES (%s, %s) RETURNING id",
                (medico_id_local, con.get('nome'))
            )
            c_id = cur.fetchone()['id']
            
            for t in con.get('telecom', []):
                cur.execute(
                    "INSERT INTO telecom (contacto_id, medico_id, tipo, valor) VALUES (%s, %s, %s, %s)",
                    (c_id, medico_id_local, t.get('tipo'), t.get('valor'))
                )
            
            e_obj = con.get('endereco')
            if e_obj and isinstance(e_obj, dict):
                cur.execute(
                    "INSERT INTO endereco (contacto_id, tipo, valor) VALUES (%s, %s, %s)",
                    (c_id, e_obj.get('tipo'), e_obj.get('valor'))
                )

        conn.commit()

        hapi_url = f"{FHIR_SERVER_URL}/Practitioner"
        headers = {"Content-Type": "application/fhir+json;charset=utf-8"}

        try:
            hapi_res = requests.post(hapi_url, json=fhir_payload, headers=headers, timeout=10)
            if hapi_res.status_code in [200, 201]:
                fhir_id_gerado = hapi_res.json().get('id')
                cur.execute("UPDATE medicos SET fhir_id = %s WHERE id = %s", (str(fhir_id_gerado), medico_id_local))
                conn.commit()

                return {
                    "status": "sucesso",
                    "id_local": medico_id_local,
                    "id_fhir": fhir_id_gerado,
                    "mensagem": "Médico criado e sincronizado!"
                }
            else:
                return {
                    "status": "aviso",
                    "id_local": medico_id_local,
                    "mensagem": "Gravado localmente, mas falhou no HAPI final.",
                    "erro_hapi": hapi_res.text[:200]
                }
        except Exception as hapi_err:
            return {
                "status": "aviso",
                "id_local": medico_id_local,
                "mensagem": "Gravado localmente, mas HAPI offline.",
                "detalhe": str(hapi_err)
            }

    except Exception as e:
        if conn: conn.rollback()
        if isinstance(e, HTTPException): raise e
        raise HTTPException(status_code=500, detail=f"Erro interno: {str(e)}")
    finally:
        if conn:
            cur.close()
            conn.close()

@app.post("/Encounter")
async def create_encounter(data: dict, current_user: str = Depends(get_current_user)):
    conn = None
    try:
        ref_paciente_local = data.get('refer_paciente', '')
        id_paciente_sql = int(ref_paciente_local.split('/')[-1]) if '/' in ref_paciente_local else None

        ref_medico_local = data.get('refer_medico', '')
        id_medico_sql = int(ref_medico_local.split('/')[-1]) if '/' in ref_medico_local else None

        if not id_paciente_sql:
            raise HTTPException(status_code=400, detail="Referência de paciente inválida.")

        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute("SELECT fhir_id FROM patients WHERE id = %s", (id_paciente_sql,))
        row_p = cur.fetchone()
        if not row_p or not row_p['fhir_id']:
            raise HTTPException(status_code=400, detail="Paciente local não sincronizado com HAPI.")
        fhir_id_paciente = row_p['fhir_id']

        fhir_id_medico = None
        if id_medico_sql:
            cur.execute("SELECT fhir_id FROM medicos WHERE id = %s", (id_medico_sql,))
            row_m = cur.fetchone()
            if row_m and row_m['fhir_id']:
                fhir_id_medico = row_m['fhir_id']

        lista_participantes = []
        if fhir_id_medico:
            lista_participantes.append({
                "individual": {"reference": f"Practitioner/{fhir_id_medico}"}
            })

        fhir_payload = {
            "resourceType": "Encounter",
            "status": "finished",
            "class": {
                "system": "http://terminology.hl7.org/CodeSystem/v3-ActCode",
                "code": "AMB", 
                "display": "ambulatory"
            },
            "subject": {"reference": f"Patient/{fhir_id_paciente}"},
            "participant": lista_participantes,
            "period": {"start": data.get('data_consulta')},
            "type": [{"text": data.get('tipo_consulta')}]
        }

        valido, mensagem = validar_recurso_fhir(fhir_payload, "Encounter")
        if not valido:
            raise HTTPException(status_code=400, detail=f"Erro no Schema Encounter: {mensagem}")

        try:
            cur.execute(
                """INSERT INTO consultas (paciente_id, medico_id, data_consulta, tipo_consulta) 
                   VALUES (%s, %s, %s, %s) RETURNING id""",
                (id_paciente_sql, id_medico_sql, data.get('data_consulta'), data.get('tipo_consulta'))
            )
            consulta_id_local = cur.fetchone()['id']

            cur.execute(
                "INSERT INTO historico (paciente_id, consulta_id) VALUES (%s, %s)",
                (id_paciente_sql, consulta_id_local)
            )
            conn.commit()
        except Exception as sql_err:
            conn.rollback()
            raise HTTPException(status_code=500, detail=f"Erro ao gravar no SQL local: {str(sql_err)}")

        hapi_url = f"{FHIR_SERVER_URL}/Encounter"
        headers = {"Content-Type": "application/fhir+json;charset=utf-8"}

        try:
            hapi_res = requests.post(hapi_url, json=fhir_payload, headers=headers, timeout=10)
            if hapi_res.status_code in [200, 201]:
                fhir_id_gerado = hapi_res.json().get('id')
                cur.execute("UPDATE consultas SET fhir_id = %s WHERE id = %s", (str(fhir_id_gerado), consulta_id_local))
                conn.commit()

                return {
                    "status": "sucesso",
                    "id_local": consulta_id_local,
                    "id_fhir": fhir_id_gerado,
                    "info": "Consulta e Histórico criados e sincronizados."
                }
            else:
                return {
                    "status": "aviso",
                    "id_local": consulta_id_local,
                    "mensagem": "Gravado localmente, mas rejeitado pelo HAPI final.",
                    "erro": hapi_res.text[:200]
                }
        except Exception as hapi_err:
            return {
                "status": "aviso", 
                "id_local": consulta_id_local, 
                "msg": "SQL OK, HAPI offline.", 
                "erro": str(hapi_err)
            }

    except Exception as e:
        if conn: conn.rollback()
        if isinstance(e, HTTPException): raise e
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            cur.close()
            conn.close()

@app.get("/Patient/{local_id}")
async def get_patient(local_id: int, current_user: str = Depends(get_current_user)):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT fhir_id FROM patients WHERE id = %s", (local_id,))
        result = cur.fetchone()

        if not result:
            raise HTTPException(status_code=404, detail="Paciente não existe no SQL")
        
        fhir_id = result.get('fhir_id')
        if not fhir_id:
            raise HTTPException(status_code=404, detail="Paciente sem sincronização HAPI")

        hapi_url = f"{FHIR_SERVER_URL}/Patient/{fhir_id}"
        headers = {"Accept": "application/fhir+json"}
        response = requests.get(hapi_url, headers=headers, timeout=5)

        if response.status_code == 200:
            return {
                "id_local": local_id,
                "id_fhir": fhir_id,
                "recurso_fhir_completo": response.json()
            }
        else:
            raise HTTPException(status_code=response.status_code, detail="Erro ao buscar no HAPI")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            cur.close()
            conn.close()

@app.get("/Observation/{local_id}")
async def get_observation(local_id: int, current_user: str = Depends(get_current_user)):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT fhir_id FROM observacoes WHERE id = %s", (local_id,))
        result = cur.fetchone()

        if not result:
            raise HTTPException(status_code=404, detail="Observação não encontrada no SQL")
        
        fhir_id = result.get('fhir_id')
        if not fhir_id:
            raise HTTPException(status_code=404, detail="Observação sem mapeamento FHIR")

        hapi_url = f"{FHIR_SERVER_URL}/Observation/{fhir_id}"
        headers = {"Accept": "application/fhir+json"}
        response = requests.get(hapi_url, headers=headers, timeout=5)

        if response.status_code == 200:
            return {
                "id_local": local_id,
                "id_fhir": fhir_id,
                "dados_provenientes_do_hapi": response.json()
            }
        else:
            raise HTTPException(status_code=response.status_code, detail="Erro no HAPI")
    except Exception as e:
        if isinstance(e, HTTPException): raise e
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn: conn.close()

@app.get("/Observation")
async def get_patient_observations(patient: int, current_user: str = Depends(get_current_user)):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT fhir_id FROM patients WHERE id = %s", (patient,))
        res_paciente = cur.fetchone()

        if not res_paciente or not res_paciente['fhir_id']:
            raise HTTPException(status_code=404, detail="Paciente não encontrado ou não sincronizado")

        fhir_patient_id = res_paciente['fhir_id']
        hapi_url = f"{FHIR_SERVER_URL}/Observation?patient={fhir_patient_id}"
        headers = {"Accept": "application/fhir+json"}
        response = requests.get(hapi_url, headers=headers, timeout=5)

        if response.status_code == 200:
            fhir_data = response.json()
            observacoes = [entry["resource"] for entry in fhir_data.get("entry", [])]
            return {
                "id_local_paciente": patient,
                "id_fhir_paciente": fhir_patient_id,
                "total_observacoes": len(observacoes),
                "lista_observacoes": observacoes
            }
        else:
            raise HTTPException(status_code=response.status_code, detail="Erro no HAPI")
    except Exception as e:
        if isinstance(e, HTTPException): raise e
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            cur.close()
            conn.close()

@app.get("/Practitioner/{local_id}")
async def get_practitioner(local_id: int, current_user: str = Depends(get_current_user)):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT fhir_id FROM medicos WHERE id = %s", (local_id,))
        result = cur.fetchone()

        if not result:
            raise HTTPException(status_code=404, detail="Médico não existe no SQL")

        fhir_id = result.get('fhir_id')
        if not fhir_id:
            raise HTTPException(status_code=404, detail="Médico não sincronizado")

        hapi_url = f"{FHIR_SERVER_URL}/Practitioner/{fhir_id}"
        headers = {"Accept": "application/fhir+json"}
        response = requests.get(hapi_url, headers=headers, timeout=5)
        
        if response.status_code == 200:
            return {
                "id_local": local_id,
                "id_fhir": fhir_id,
                "recurso_fhir_do_servidor": response.json()
            }
        else:
            raise HTTPException(status_code=response.status_code, detail="Erro no HAPI")
    except Exception as e:
        if isinstance(e, HTTPException): raise e
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            cur.close()
            conn.close()

@app.get("/Encounter/{consulta_id}")
async def get_encounter(consulta_id: int, current_user: str = Depends(get_current_user)):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT fhir_id FROM consultas WHERE id = %s", (consulta_id,))
        result = cur.fetchone()

        if not result:
            raise HTTPException(status_code=404, detail="Consulta não encontrada no SQL")
        
        fhir_id = result.get('fhir_id')
        if not fhir_id:
            raise HTTPException(status_code=404, detail="Consulta sem fhir_id")

        hapi_url = f"{FHIR_SERVER_URL}/Encounter/{fhir_id}"
        headers = {"Accept": "application/fhir+json"}
        response = requests.get(hapi_url, headers=headers, timeout=5)
        
        if response.status_code == 200:
            return {
                "id_local": consulta_id,
                "id_fhir_no_hapi": fhir_id,
                "recurso_fhir_completo": response.json()
            }
        else:
            raise HTTPException(status_code=response.status_code, detail="Erro no HAPI")
    except Exception as e:
        if isinstance(e, HTTPException): raise e
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            cur.close()
            conn.close()

@app.get("/Patient/{local_id}/History")
async def get_patient_history(local_id: int, current_user: str = Depends(get_current_user)):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT fhir_id FROM patients WHERE id = %s", (local_id,))
        result = cur.fetchone()
        
        if not result or not result['fhir_id']:
            raise HTTPException(status_code=404, detail="Paciente não encontrado ou sem ID FHIR")

        fhir_id = result['fhir_id']
        hapi_url = f"{FHIR_SERVER_URL}/Observation?subject=Patient/{fhir_id}"
        response = requests.get(hapi_url)

        if response.status_code == 200:
            return response.json()
        else:
            raise HTTPException(status_code=response.status_code, detail="Erro no HAPI")
    finally:
        if conn: conn.close()