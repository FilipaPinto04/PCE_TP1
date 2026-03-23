import json
from fastapi import FastAPI

app = FastAPI()

@app.get("/Patient")
async def read_patient():
    # Lê o ficheiro local que contém o recurso FHIR Patient [cite: 8, 22]
    try:
        with open("exemplo_post_paciente.json", "r", encoding='utf-8') as f:
            dados_paciente = json.load(f)
        
        # Devolve o conteúdo do ficheiro diretamente [cite: 18, 23]
        return dados_paciente
    except FileNotFoundError:
        return {"error": "Ficheiro exemplo_post_paciente.json não encontrado"}