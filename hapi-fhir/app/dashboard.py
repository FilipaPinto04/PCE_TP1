import streamlit as st
import requests
import pandas as pd
import psycopg2
from psycopg2.extras import RealDictCursor

# Configuração da página do Streamlit
st.set_page_config(page_title="Portal Clínico Integrado", layout="wide")

EHRBASE_URL = "http://localhost:8082/ehrbase/rest/openehr/v1"

# 🗄️ Função para ligar à vossa Base de Dados SQL local
# Como o Streamlit corre no teu PC, ligamos à porta 5432 (ou à porta mapeada no teu localhost)
def get_db_connection():
    return psycopg2.connect(
        host="localhost",  
        port=5432, # Ajusta para 5434 se for a porta mapeada do ehrdb, ou mantém 5432 para a db do TP1
        database="tp1",
        user="admin",
        password="admin"
    )

def obter_headers_seguros():
    return {
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

st.title("🏥 Portal Clínico Hospitalar (openEHR + FHIR)")
st.subheader("Desafio Extra Avançado: Sinais Vitais, Consultas e Filtro por Corpo Médico")
st.markdown("---")

# Zona de Introdução de Dados
numero_utente_input = st.text_input("Introduza o N.º de Utente do SNS (ou o ID local se vazio):", placeholder="Ex: 123456789")

if numero_utente_input:
    # 1. Procurar o paciente na vossa DB local para descobrir o ID interno
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    # Procura por numero_utente ou pelo ID diretamente caso usem a Opção A
    cur.execute("SELECT id, nome, numero_utente FROM patients WHERE numero_utente = %s OR id::text = %s", (numero_utente_input, numero_utente_input))
    paciente = cur.fetchone()
    
    if not paciente:
        st.error("❌ Paciente não localizado na base de dados relacional local.")
        cur.close()
        conn.close()
    else:
        paciente_id_local = paciente['id']
        nome_paciente = paciente['nome']
        num_sns_real = paciente['numero_utente'] if paciente['numero_utente'] else str(paciente_id_local)
        
        st.write(f"### 🧑 Paciente: **{nome_paciente}** | N.º Utente: `{num_sns_real}`")
        
        # Criar Separadores (Tabs) no Streamlit para organizar a informação
        tab_sinais, tab_consultas = st.tabs(["📈 Sinais Vitais (openEHR)", "📅 Histórico de Consultas (FHIR/SQL)"])
        
        # ==========================================
        # TAB 1: SINAIS VITAIS (QUERY AQL AO EHRBASE)
        # ==========================================
        with tab_sinais:
            with st.spinner("A consultar repositório openEHR..."):
                NAMESPACE = "pt-sns-utente"
                search_url = f"{EHRBASE_URL}/ehr?subject_id={num_sns_real}&subject_namespace={NAMESPACE}"
                
                try:
                    res_ehr = requests.get(search_url, headers=obter_headers_seguros())
                    if res_ehr.status_code != 200:
                        st.info("ℹ️ Este utente ainda não possui um EHR_ID ativo ou registos clínicos no EHRbase.")
                    else:
                        ehr_id = res_ehr.json()['ehr_id']['value']
                        st.caption(f"EHRbase ID: `{ehr_id}`")
                        
                        aql_query = {
                            "q": f"""
                            SELECT
                                c/context/start_time/value as data_hora,
                                o/name/value as sinal_vital,
                                o/data[at0001]/events[at0002]/data[at0003]/items[at0006]/value/numerator as valor_proporcao,
                                o/data[at0002]/events[at0003]/data[at0001]/items[at0004]/value/magnitude as valor_quant,
                                o/data[at0002]/events[at0003]/data[at0001]/items[at0004]/value/units as unidade_quant,
                                o/data[at0001]/events[at0006]/data[at0003]/items[at0004]/value/magnitude as valor_sistolica,
                                o/data[at0001]/events[at0006]/data[at0003]/items[at0004]/value/units as unidade_sistolica,
                                o/data[at0001]/events[at0006]/data[at0003]/items[at0005]/value/magnitude as valor_diastolica,
                                o/data[at0001]/events[at0006]/data[at0003]/items[at0005]/value/units as unidade_diastolica
                            FROM EHR [ehr_id/value='{ehr_id}']
                            CONTAINS COMPOSITION c
                            CONTAINS OBSERVATION o
                            ORDER BY c/context/start_time/value DESC
                            """
                        }
                        
                        res_aql = requests.post(f"{EHRBASE_URL}/query/aql", json=aql_query, headers=obter_headers_seguros())
                        
                        if res_aql.status_code == 200:
                            rows = res_aql.json().get('rows', [])
                            if not rows:
                                st.info("ℹ️ Sem observações de sinais vitais processadas pelo worker.")
                            else:
                                dados_limpos = []
                                for row in rows:
                                    data_formatada = row[0].replace('T', ' ').split('.')[0] if row[0] else "N/D"
                                    tipo_sinal = row[1]
                                    
                                    if tipo_sinal == "SpO2" and row[2] is not None:
                                        valor, unidade = row[2], "%"
                                    elif tipo_sinal == "Systolic" and row[5] is not None:
                                        valor, unidade = row[5], row[6]
                                    elif tipo_sinal == "Diastolic" and row[7] is not None:
                                        valor, unidade = row[7], row[8]
                                    elif row[3] is not None:
                                        valor, unidade = row[3], row[4]
                                    else:
                                        continue
                                        
                                    dados_limpos.append({
                                        "Data/Hora": data_formatada,
                                        "Métrica Clínica": tipo_sinal,
                                        "Valor": float(valor),
                                        "Unidade": unidade
                                    })
                                
                                df_sinais = pd.DataFrame(dados_limpos)
                                
                                metricas_disponiveis = df_sinais["Métrica Clínica"].unique()
                                metrica_selecionada = st.selectbox("Selecione o Sinal Vital para o gráfico:", metricas_disponiveis)
                                
                                df_filtrado = df_sinais[df_sinais["Métrica Clínica"] == metrica_selecionada].sort_values("Data/Hora")
                                if not df_filtrado.empty:
                                    st.line_chart(data=df_filtrado, x="Data/Hora", y="Valor")
                                
                                st.dataframe(df_sinais, use_container_width=True)
                except Exception as e:
                    st.error(f"Erro de comunicação openEHR: {e}")
        
        # ==========================================
        # TAB 2: HISTÓRICO DE CONSULTAS E FILTRO MÉDICO
        # ==========================================
        with tab_consultas:
            st.markdown("### 📅 Consultas Agendadas e Realizadas")
            
            # Executa um JOIN entre consultas e medicos para trazer o histórico completo do paciente
            query_consultas = """
                SELECT 
                    c.id as consulta_id,
                    c.data_consulta,
                    c.tipo_consulta,
                    m.nome as nome_medico,
                    m.especialidade
                FROM consultas c
                JOIN medicos m ON c.medico_id = m.id
                WHERE c.paciente_id = %s
                ORDER BY c.data_consulta DESC
            """
            cur.execute(query_consultas, (paciente_id_local,))
            consultas_rows = cur.fetchall()
            
            if not consultas_rows:
                st.info("ℹ️ Este utente ainda não tem histórico de consultas (Encounters) registadas no sistema.")
            else:
                # Converte para DataFrame para podermos manipular no Streamlit
                df_consultas = pd.DataFrame(consultas_rows)
                
                # Renomeia as colunas para o utilizador ler melhor
                df_consultas.columns = ["ID Consulta", "Data da Consulta", "Tipo / Especialidade", "Médico Assistente", "Especialidade Médica"]
                
                # 🔍 FUNCIONALIDADE DE FILTRO: Obter lista de médicos únicos que já atenderam este doente
                lista_medicos = ["Todos os Médicos"] + list(df_consultas["Médico Assistente"].unique())
                medico_selecionado = st.selectbox("🔍 Filtrar histórico por Médico Assistente:", lista_medicos)
                
                # Aplica o filtro dinamicamente
                if medico_selecionado != "Todos os Médicos":
                    df_consultas_filtrado = df_consultas[df_consultas["Médico Assistente"] == medico_selecionado]
                else:
                    df_consultas_filtrado = df_consultas
                
                # Apresenta o resultado estruturado
                st.write(f"Exibindo **{len(df_consultas_filtrado)}** registo(s) de consulta.")
                st.dataframe(df_consultas_filtrado, use_container_width=True)
                
    cur.close()
    conn.close()