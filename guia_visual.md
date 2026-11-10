# 🚀 Guia Descomplicado: Criando sua Inteligência Artificial que Nunca Cai (MCP na OCI)

Bem-vindo! Se você não é um especialista em nuvem, não se preocupe. Este guia foi feito para você. Vamos pegar na sua mão e construir uma arquitetura de IA que funciona em duas cidades ao mesmo tempo (São Paulo e Vinhedo). Se uma falhar, a outra assume na hora!

### 🗺️ O Mapa do Tesouro (O que vamos construir?)
Nossa jornada tem 5 grandes fases:
1. **Os Trabalhadores (OKE):** Preparar os computadores em SP e Vinhedo.
2. **Os Serviços:** Contratar o "Cérebro" da IA, arrumar a rede e o endereço do site.
3. **A Memória (Cache):** Criar o caderno onde a IA anota a conversa.
4. **Preparativos Finais:** Empacotar nosso aplicativo e criar os cadeados de segurança.
5. **O Grande Teste:** Colocar no ar e puxar a tomada para ver a mágica acontecer!

---

## 🏗️ FASE 1: Preparando o Terreno e os Trabalhadores

### Passo 1: Escolhendo nosso "Terreno" na Nuvem
*Na nuvem, chamamos nosso espaço isolado de "Compartment". É como se fosse uma pasta no seu computador onde guardaremos tudo deste projeto.*

**O que fazer na tela (Console OCI):**
* [ ] No topo da tela, clique na região e escolha **Brazil East (São Paulo)**.
* [ ] Vá no menu lateral > **Identity & Security** > **Compartments**.
* [ ] Clique em **[ Create Compartment ]** e dê o nome de `mcp-lab`.
* [ ] Anote o **OCID** (um código longo que aparece na tela). Ele é o RG da sua pasta.
* [ ] Certifique-se de que consegue ver as regiões **São Paulo** e **Vinhedo** no menu do topo.

### Passo 2: Preparando sua "Caixa de Ferramentas" (Seu Computador)
*Vamos usar uma tela preta (Terminal) para rodar alguns comandos que farão o trabalho pesado automaticamente.*

**O que fazer (No Terminal do seu computador):**
* [ ] Abra o Terminal e cole os comandos abaixo (aperte ENTER no final):
```bash
git clone https://github.com/SilvioCristiano/Resilient-HA-OCI-MCP.git
cd Resilient-HA-OCI-MCP
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-tools.txt
```
💡 *Por que? Isso apenas baixa os arquivos do projeto e prepara seu computador para rodar os scripts do Silvio.*

### Passo 3: Contratando os Trabalhadores em São Paulo (OKE)
*O OKE é o gerente que cuida dos servidores que manterão o chat no ar.*

**O que fazer na tela (Console OCI em São Paulo):**
* [ ] Vá em **Developer Services** > **Kubernetes Clusters (OKE)**.
* [ ] Clique em **[ Create Cluster ]** e escolha **Quick Create**.
* [ ] Preencha:
  * **Nome:** `mcp-resilience-gru`
  * **Compartment:** `mcp-lab`
  * **Tipo:** Basic *(importante para economizar!)*
  * **Node Count:** `2` (Queremos 2 trabalhadores)
  * **Acesso:** Privado (Private Workers / Private API)

### Passo 4: Contratando os Trabalhadores em Vinhedo (OKE Secundário)
*Se SP cair, Vinhedo assume. Precisamos de uma equipe lá também.*

**O que fazer na tela (Console OCI):**
* [ ] Mude a região no topo para **Brazil Southeast (Vinhedo)**.
* [ ] Repita exatamente o Passo 3, mas mude o nome para: `mcp-resilience-vcp`.

### Passo 5: A Caça ao Tesouro (Anotando IDs)
*Precisamos anotar os "CPFs" dos recursos criados para avisar aos nossos scripts onde eles estão.*

**O que fazer (em SP e depois em Vinhedo):**
* [ ] Abra os detalhes do cluster criado.
* [ ] Anote os IDs (OCIDs) do Cluster, da Rede Virtual (VCN) e das máquinas (Workers).
* [ ] Crie um bloquinho de notas no seu computador com esses códigos. Vamos usá-los no Passo 12!

---

## 🔒 FASE 2: Preparando as Portas, a IA e o Endereço

### Passos 6 e 7: Os Seguranças da Memória (NSG)
*Nossa memória (Cache) tem dados sigilosos. Vamos criar um "Segurança de Porta" (NSG) que só deixa nossos trabalhadores entrarem.*

**O que fazer na tela (Console OCI):**
* [ ] Vá em **Networking** > **Virtual Cloud Networks**. Clique na sua VCN.
* [ ] Vá em **Network Security Groups** e crie um chamado `mcp-cache-gru` (em SP) e outro `mcp-cache-vcp` (em Vinhedo).
* [ ] Adicione uma regra de **Entrada (Ingress)**:
  * Protocolo: TCP
  * Porta de Destino: `6379`
  * Origem: O endereço IP da rede dos seus trabalhadores (CIDR).
* [ ] Anote o ID (OCID) desse segurança!

### Passo 8: Abrindo as Portas para o Público (Load Balancers)
*O Load Balancer é a nossa "Recepção". Ele recebe os clientes da internet e joga para os trabalhadores.*

💡 *Nesta etapa, você só precisa garantir nas regras da sua rede (Security Lists) que a porta `443` (Internet segura) está aberta para entrar, e as portas internas (`32443`, `10256`, `32380`) podem conversar entre si.*

### Passos 9 e 10: Contratando o Cérebro (A Inteligência Artificial)
*Vamos escolher qual modelo de IA vai responder às perguntas.*

**O que fazer na tela:**
* [ ] Em **São Paulo**, vá em **Analytics and AI** > **Generative AI**.
* [ ] Anote o OCID do modelo `meta.llama-3.3-70b-instruct`. Ele será nosso "Cérebro".
* [ ] Em **Vinhedo**, vá na mesma tela, mas em **Routing profiles**.
* [ ] Crie um perfil apontando para São Paulo. Isso diz: *"Se perguntarem algo em Vinhedo, peça pro cérebro de SP responder"*. Anote o ID deste perfil.

### Passo 11: Colocando a Placa na Fachada (DNS e Domínio)
*Precisamos de um endereço bonito como `mcp.seusite.com`.*

**O que fazer:**
* [ ] Vá em **Networking** > **DNS Management** > **Zones**.
* [ ] Crie uma Zona Pública com o nome do seu site (ex: `seusite.com`).
* [ ] Copie os 4 servidores (Nameservers) que a Oracle te der e coloque lá onde você comprou o domínio (ex: Registro.br, HostGator).

### Passos 12 e 13: Entregando o Mapa para os Scripts (Config e IAM)
*Agora vamos pegar todos aqueles IDs que anotamos e colocar no arquivo de configuração do projeto.*

**O que fazer (No Terminal):**
* [ ] Rode: `cp config/lab.example.json config/lab.json` e depois `python scripts/lab.py init`
* [ ] Abra o arquivo `config/lab.json` em um bloco de notas.
* [ ] Substitua todos os "REPLACE_ME" pelos IDs que você anotou nos passos anteriores (IDs do cluster, da rede, do modelo, etc).
* [ ] De volta ao terminal, rode os comandos para dar permissão (crachá) aos nossos trabalhadores:
```bash
python scripts/cloud.py iam
python scripts/cloud.py iam --apply
```

---

## 🧠 FASE 3: Criando a Memória da IA (OCI Cache)

### Passos 14, 15 e 16: O Caderno de Anotações
*Se SP cair, Vinhedo precisa saber o que o cliente estava conversando. Vamos usar o Redis (Cache) para guardar essas anotações nas duas cidades.*

**O que fazer (No Terminal):**
* [ ] Crie a memória principal em SP:
```bash
python scripts/cloud.py cache --region sa-saopaulo-1 --apply
```
* [ ] Crie a memória cópia em Vinhedo:
```bash
python scripts/cloud.py cache --region sa-vinhedo-1 --apply
```

💡 *Pronto! Agora SP anota a conversa e automaticamente tira uma xerox para Vinhedo. Vá no console da OCI (Databases > OCI Cache) e confira se estão com status "Active".*

---

## 📦 FASE 4: Preparando o Lançamento

### Passos 17 e 18: O Túnel Secreto (Bastion)
*Por segurança, nossos trabalhadores (OKE) estão isolados da internet. Vamos criar um túnel temporário para podermos enviar os arquivos do aplicativo.*

**O que fazer na tela e Terminal:**
* [ ] No Console OCI, crie um **Bastion** em SP e outro em Vinhedo.
* [ ] No terminal, rode os comandos do projeto para conectar o seu computador ao OKE usando esse túnel secreto:
```bash
python scripts/private_access.py --region sa-saopaulo-1 --port 16443
python scripts/private_access.py --region sa-vinhedo-1 --port 26443
export KUBECONFIG="$PWD/.runtime/kubeconfig-gru:$PWD/.runtime/kubeconfig-vcp"
```

### Passo 19: Colocando o Cadeado (Certificado HTTPS)
*Vamos garantir que o site tenha aquele cadeado verde de segurança.*

**O que fazer (No Terminal):**
* [ ] Rode: `python scripts/issue_public_tls.py --agree-terms`
💡 *Isso vai conversar com a internet e gerar um certificado seguro para o seu site automaticamente!*

### Passo 20: Empacotando o Aplicativo (Docker)
*Vamos transformar nosso código de chat em "caixas" (imagens) prontas para rodar.*

**O que fazer (No Terminal):**
* [ ] Rode:
```bash
python scripts/cloud.py registry --apply
python scripts/lab.py build
```
💡 *Isso constrói o aplicativo no seu computador e manda as caixas lá para a nuvem da Oracle.*

---

## 🚀 FASE 5: Publicando e Testando o Caos!

### Passos 21 e 22: Colocando no Ar (Deploy)
*Chegou a hora! Vamos ligar o aplicativo nas duas cidades.*

**O que fazer (No Terminal):**
* [ ] Ligue São Paulo:
```bash
python scripts/lab.py render --region sa-saopaulo-1
python scripts/lab.py deploy --region sa-saopaulo-1 --apply
python scripts/lab.py discover
```
* [ ] Ligue Vinhedo e atualize ambos:
```bash
python scripts/lab.py render --region sa-vinhedo-1
python scripts/lab.py deploy --region sa-vinhedo-1 --apply
python scripts/lab.py deploy --region sa-saopaulo-1 --apply
```

### Passo 23: O Guarda de Trânsito Global (DNS Failover)
*Vamos configurar a regra de ouro: O Tráfego vai para SP. Se SP quebrar, desvie para Vinhedo!*

**O que fazer (No Terminal):**
* [ ] Rode: `python scripts/cloud.py dns --apply`

💡 *Você pode testar abrindo o navegador no endereço do seu site (ex: `https://mcp.seusite.com/readyz`). Se aparecer mensagem de sucesso, o site está no ar!*

### Passo 24: O Ensaio Geral (Teste de Queda)
*Vamos puxar a tomada de São Paulo e ver se a IA lembra da conversa!*

**O que fazer (No Navegador):**
1. Abra `https://seusite.com/chat/`. Comece a conversar com a IA (ex: "Meu nome é Silvio e meu projeto é o MCP").
2. Abra outra aba no endereço `/chat/presenter` (use a senha que está no arquivo `.runtime/presenter-access.txt` no seu computador).
3. Aperte o botão vermelho gigante **[ Interromper São Paulo ]**.
4. Volte na aba do chat e pergunte: "Qual é o meu nome e o meu projeto?".
5. **A Mágica:** A IA vai responder de Vinhedo, sem esquecer de nada, em questão de segundos!

🎉 **Parabéns! Você construiu uma arquitetura de IA à prova de desastres!**