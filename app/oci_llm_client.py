"""OCI-only inference; the Vinhedo routing profile must target São Paulo only."""
import os
import oci

REGIONS = ("sa-saopaulo-1", "sa-vinhedo-1")


class OCILLMClient:
    def __init__(self):
        self.compartment = os.environ["OCI_COMPARTMENT_ID"]
        self.models = {REGIONS[0]: os.environ["OCI_MODEL_ID"],
                       REGIONS[1]: os.environ["OCI_VCP_ROUTING_PROFILE_ID"]}
        if os.getenv("OCI_AUTH", "instance_principal") == "instance_principal":
            config, signer = {}, oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
        else:
            config = oci.config.from_file(profile_name=os.getenv("OCI_PROFILE", "DEFAULT"))
            signer = None
        self.clients = {}
        for region in REGIONS:
            kwargs = dict(service_endpoint=f"https://inference.generativeai.{region}.oci.oraclecloud.com",
                          timeout=(1, 30), retry_strategy=oci.retry.NoneRetryStrategy())
            if signer:
                kwargs["signer"] = signer
            self.clients[region] = oci.generative_ai_inference.GenerativeAiInferenceClient(
                dict(config, region=region), **kwargs)

    def chat(self, history):
        m = oci.generative_ai_inference.models
        messages = [m.Message(role=x["role"].upper(), content=[m.TextContent(text=x["text"])])
                    for x in history]
        for region in REGIONS:
            try:
                response = self.clients[region].chat(m.ChatDetails(
                    compartment_id=self.compartment,
                    serving_mode=m.OnDemandServingMode(model_id=self.models[region]),
                    chat_request=m.GenericChatRequest(messages=messages, max_tokens=256,
                                                      temperature=0)))
                text = "".join(c.text for c in response.data.chat_response.choices[0].message.content
                               if getattr(c, "text", None))
                if not text:
                    raise RuntimeError("Empty inference response")
                return {"answer": text, "inference_api_region": region,
                        "model_execution_region": REGIONS[0],
                        "request_id": response.headers.get("opc-request-id")}
            except oci.exceptions.ServiceError as exc:
                if exc.status not in (429, 500, 502, 503, 504) or region == REGIONS[-1]:
                    raise
            except (oci.exceptions.RequestException, oci.exceptions.ConnectTimeout):
                if region == REGIONS[-1]:
                    raise
