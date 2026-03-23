from fastapi import FastAPI, status

app = FastAPI()

# 2 - Exposição da rota GET /healthcheck
@app.get("/healthcheck") #de cada vez que é feito um pedido get no /healthcheck devolve a funcao
async def health_check():
    """
    Retorna o estado de saúde da aplicação.
    Útil para monitoramento e Load Balancers.
    """
    return {"status": "healthy", "message": "Serviço rodando perfeitamente"}