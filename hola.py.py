import asyncio
import logging
import websockets
import random
import os
import json
from datetime import datetime,timedelta
#import datetime
from ocpp.v16 import ChargePoint as cp
from ocpp.v16 import call, call_result
from ocpp.v16.enums import RegistrationStatus
from ocpp.v16.enums import Reason
from ocpp.v16.enums import ChargePointStatus, ChargePointErrorCode
from collections import defaultdict
from ocpp.v16.enums import ChargingProfilePurposeType
from ocpp.routing import on  # Importa el decorador 'on'
import board
import busio
from digitalio import DigitalInOut
from decimal import Decimal


#########################################RS485###########################################
#########################################################################################

import minimalmodbus
import time

#
# NOTE: pick the import that matches the interface being used
#
from adafruit_pn532.i2c import PN532_I2C

import minimalmodbus
import time
import sys
import board
import busio

#########################################################################################
#########################################################################################

# Estructura para almacenar perfiles de carga por conector y stackLevel
charging_profiles = defaultdict(lambda: defaultdict(list))

# Estructura para almacenar los perfiles ejecutados
executed_profiles = []

# Definir la ruta del archivo JSON en el mismo directorio que el script
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
JSON_PATH = os.path.join(BASE_DIR, 'estado.json')

#########################################################################################
#########################################################################################

# Configuracion del logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

    # Funcion para convertir Decimal a float para JSON
def decimal_default(obj):
    if isinstance(obj, Decimal):
        return float(obj)
    raise TypeError("Object of type %s is not JSON serializable" % type(obj))


class ChargePoint(cp):
    def __init__(self,charge_point_id, websocket):
        super().__init__(charge_point_id, websocket)

        # Configuration RS485
        self.slave_address = 1  # Adjust the Modbus slave address according to your setup
        self.serial_port = '/dev/ttyUSB0'  # Adjust the serial port based on your configuration
        self.baudrate = 9600  # Adjust the baudrate based on your Modbus device's specifications
        self.instrument = minimalmodbus.Instrument(self.serial_port, self.slave_address)
        self.instrument.serial.baudrate = self.baudrate
        self.instrument.serial.timeout = 1  # Set the timeout (in seconds) as needed  
        # Configuraci del bus I2C
        self.i2c = busio.I2C(board.SCL, board.SDA)
        ## Codigos I2C
        self.readEmpalme = 254
        self.readEstado = 253
        self.setPwmToUc = 3
        self.bufferRead = bytearray(1) 
        self.reset_pin = DigitalInOut(board.D6)
        # On Raspberry Pi, you must also connect a pin to P32 "H_Request" for hardware
        # wakeup! this means we don't need to do the I2C clock-stretch thing
        self.req_pin = DigitalInOut(board.D12)
        self.pn532 = PN532_I2C(self.i2c,address=0x24, debug=False, reset=self.reset_pin, req=self.req_pin)   
        # Direcci I2C del esclavo (Bluepill)
        self.slave_address_I2c = 0x08
        self.devices = 0
        ##################################################
        self.stateFromMicro=0
        self.stateToMicro=0
        self.state=0
        self.startComm=False
        self.startMeasure=False
        self.heartBeatAuthorization=False
        self.timeDelayheartBeat = 60
        self.bootNotification=False
        self.waiting_for_trigger_message = False  # Nuevo estado para esperar TriggerMessage
        # Inicializa Availability como Available
        self.availability = "Operative"
        # Nuevo flag para controlar el envio de BootNotification
        self.boot_notification_sent = False 
        self.transaction_id = None  # Inicializar transaction_id como None
        self.meter=0
        self.meter_start = 0
        self.meter_stop = 0
        self.transaction_start_time = None
        self.transaction_stop_time = None
        self.pending_change_availability = None  # Nueva variable para almacenar cambios programados
        self.flagAvailability=False
        self.authorize_remote_tx_requests = True
        self.remoteStartTransaction=False
        self.rfid_code=None
        self.sessionOn=False
        self.conect_disconect="Disconect"
        
        
        
        #############################################

    #####################   OCPP  ################################ 
    #####################   OCPP  ################################    
    # Enviar el BootNotification
    async def send_boot_notification(self):
        if self.boot_notification_sent:
            logger.info("BootNotification ya fue enviado y aceptado. No se enviara nuevamente.")
            return True
        
                # Cargar parametros desde el archivo JSON
        try:
            with open("config.json", "r") as file:
                config_data = json.load(file)
        except FileNotFoundError:
            logger.error("Archivo de configuracion JSON no encontrado.")
            return False
        except json.JSONDecodeError:
            logger.error("Error al decodificar el archivo JSON.")
            return False
        charge_point_model1=config_data.get("ChargePointModel", "EV2")
        logger.info("Probando jason dante:")
        logger.info(charge_point_model1)
        # Crear la solicitud BootNotification con parametros del JSON
        request = call.BootNotification(
            charge_point_model=config_data.get("ChargePointModel", "EV2"),
            charge_point_vendor=config_data.get("ChargePointVendor", "E2tech"),
            charge_point_serial_number=config_data.get("ChargePointSerialNumber", "0"),
            firmware_version=config_data.get("FirmwareVersion", "V1")
        )
        
        try:
            response = await self.call(request)
            logger.info(f"BootNotification response: {response.status}")
    
            if response.status == RegistrationStatus.accepted:
                logger.info("BootNotification accepted: Connected to central system.")
                self.stateToMicro = await self.readFromUc(codigo=45)

                # Verificar si la respuesta tiene un intervalo y asignarlo al heartbeat
                if hasattr(response, 'interval'):
                    self.timeDelayheartBeat = getattr(response, 'interval', self.timeDelayheartBeat)
                    logger.info(f"Heartbeat interval set to {self.timeDelayheartBeat} seconds.")
                else:
                    logger.info(f"No interval provided, using default Heartbeat interval: {self.timeDelayheartBeat} seconds.")
                 # Verificar si hay una hora en currentTime y ajustarla
                current_time = getattr(response, 'currentTime', None)
                if current_time:
                    ajustar_reloj(current_time)
                    logger.info(f"Se ajustara la hora a {current_time}.")
                else:
                    logger.info(f"No se ajustara la hora.")
                
                estado = await self.load_state()  # Cargar el estado al iniciar
                print(f"Estado despues del boot: {estado}")  # Imprimir el estado inicial
                self.availabilty=str(estado)
                self.availability2=self.availabilty
                logger.info("Self availability2 despues del boot:")
                logger.info(self.availability2)
                if self.availability2 == "Inoperative":
                    logger.info("Entre por inoperative")
                    self.stateToMicro = await self.readFromUc(codigo=90)
                    await self.display_inoperative_screen()
                    #await self.send_status_notification(connector_id=1, status="Unavailable")
                    await self.enable_main_functions()
                else:
                    logger.info("Entre por operative")
                    #await self.send_status_notification(connector_id=1, status="Preparing")
                    asyncio.create_task(self.validate_rfid())
                    await self.enable_main_functions()
                return True
            elif response.status == RegistrationStatus.pending:
                logger.info("BootNotification pending: Waiting for TriggerMessage...")
                self.waiting_for_trigger_message = True
                return False  # Pendiente, esperar hasta TriggerMessage
            elif response.status == RegistrationStatus.rejected:
            # Usa 'interval' o un valor predeterminado
                retry_interval = response.interval if hasattr(response, 'interval') else 60  
                logger.warning(f"BootNotification rejected. Retrying in {retry_interval} seconds...")
                await asyncio.sleep(retry_interval)  # Esperar antes de reintentar
                await self.send_boot_notification()  # Reintentar envio
                return False  # Reintento
    
        except Exception as e:
            logger.error(f"Failed to send BootNotification: {e}")
            return False
            
    # Enviar el Heartbeath      
    async def send_heartbeat(self):
        while True:
            request = call.Heartbeat()
            try:
                await self.call(request)
                logger.info("Heartbeat sent")
            except Exception as e:
                logger.error(f"Failed to send Heartbeat: {e}")
            await asyncio.sleep(self.timeDelayheartBeat)
            
            
    # Enviar el mensaje OCPP Authorize
    async def send_authorize_rfid(self, id_tag):
        try:
            request = call.Authorize(
                id_tag=id_tag
            )
            response = await self.call(request)
            
            if response.id_tag_info['status'] == "Accepted":
                logger.info(f"RFID {id_tag} autorizado.")
                return response.id_tag_info['status'] 
            else:
                logger.warning(f"RFID {id_tag} no autorizado. Estado: {response.id_tag_info['status']}")
                return response.id_tag_info['status'] 
        
        except Exception as e:
            logger.error(f"Error al enviar mensaje Authorize: {e}")
            
    # Enviar el mensaje OCPP Authorize Remote
    async def send_remoteAuthorize_rfid(self, id_tag1):
        try:
            request = call.Authorize(
                id_tag=id_tag1
            )
            response = await self.call(request)    
            if response.id_tag_info['status'] == "Accepted":
                self.stateToMicro = await self.readFromUc(codigo=5)
                logger.info(f"RFID {id_tag1} aceptado. Estado: idle..Conecte el cargador..")
                await self.simulate_charger_connection()  # Simular la conexion de la toma del cargador 
            else:
                print(f"RFID {rfid_code} invalido, intente nuevamente.")
                        
        except Exception as e:
            logger.error(f"Error al enviar mensaje Authorize: {e}")


    # Enviar el mensaje OCPP startTransaction
    async def start_transaction(self):
        try:
            self.meter=await self.lecturaMedidor()
            logger.info(self.meter)
            self.transaction_start_time = datetime.utcnow().isoformat() + 'Z'
            # Simular el envio de un startTransaction
            request = call.StartTransaction(
                connector_id=1,  # Ejemplo, el conector 1
                id_tag=self.rfid_code,  # RFID usado para iniciar la transaccion
                meter_start=self.meter_start,
                timestamp=self.transaction_start_time
            )
            response = await self.call(request) 
                # Leer el transactionId desde la respuesta del servidor OCPP
            self.transaction_id = response.transaction_id
            
            if response.id_tag_info['status'] == "Accepted":
                logger.info("Transaccion iniciada exitosamente. Estado: cargando.")
                #await self.send_status_notification(connector_id=1, status="Charging")
                await self.ask_to_stop_transaction()  # Preguntar si detener la transaccion
            else:
                logger.warning(f"Transaccion no aceptada. Estado: {response.id_tag_info['status']}")
                self.stateToMicro = await self.readFromUc(codigo=45)
                asyncio.create_task(self.validate_rfid()) 
        
        except Exception as e:
            logger.error(f"Error al enviar mensaje StartTransaction: {e}")
            
    # Funcion para detener la transaccion
    async def stop_transaction(self):
        try:
            self.meter=await self.lecturaMedidor()
            logger.info(self.meter)
            logger.info("El transactionID para stop trnasaction es:")
            logger.info(self.transaction_id)
            self.transaction_stop_time = datetime.utcnow().isoformat() + 'Z'
            # Simular el envio de un stopTransaction
            if self.remoteStartTransaction:
                request = call.StopTransaction(
                    id_tag=self.rfid_code,
                    meter_stop=self.meter_stop,  # Ejemplo de valor de contador final
                    timestamp=self.transaction_stop_time,
                    transaction_id=self.transaction_id,
                    reason="DeAuthorized"
                )
                self.remoteStartTransaction=False
            else:
                request = call.StopTransaction(
                    id_tag=self.rfid_code,
                    meter_stop=self.meter_stop,  # Ejemplo de valor de contador final
                    timestamp=self.transaction_stop_time,
                    transaction_id=self.transaction_id,
                    reason="DeAuthorized"
                )
            response = await self.call(request)
            logger.info("StopTransaction response: %s", response)
            if response.id_tag_info['status'] == "Accepted":
                logger.info("Transaccion detenida exitosamente.")
                self.rfid_code=None
                self.transaction_id=None
                self.sessionOn=False
                #await self.send_status_notification(connector_id=1, status="Finishing")
                self.stateToMicro = await self.readFromUc(codigo=13)
                await asyncio.sleep(3)
                self.stateToMicro = await self.readFromUc(codigo=14)
                await self.Resume()  # Mostrar el resumen despues de detener la transaccion
                # Si hay un cambio de disponibilidad programado, aplicarlo despues de la transaccion
                            # Verificar si hay un reset pendiente
                if self.pending_reset:
                    logger.info("Reset Pendiente en Stop Transaction")
                    await self.perform_reset(self.pending_reset)  # Ejecutar el reset pendiente
                    self.pending_reset = None  # Limpiar el reset pendiente
                if self.pending_change_availability:
                    self.availability = "Inoperative" if self.pending_change_availability == "Inoperative" else "Operative"
                    
                    logger.info(f"Aplicado el cambio de disponibilidad: {self.availability}")
                    self.pending_change_availability = None
                    if self.availability == "Inoperative":                        
                        logger.info("Estado de disponibilidad cambiado a Inoperativo")
                        self.stateToMicro = await self.readFromUc(codigo=90)
                        await self.display_inoperative_screen()  # Mostrar pantalla de inoperativo
                    elif self.availability == "Operative":                        
                        logger.info("Estado de disponibilidad cambiado a Operativo")
                        self.stateToMicro = await self.readFromUc(codigo=5)
                        asyncio.create_task(self.validate_rfid()) 
                    return True
            else:
                logger.warning(f"StopTransaction no aceptado. Estado: {response.id_tag_info['status']}")
                
            # Verificar si hay un reset pendiente
            if self.pending_reset:
                logger.info("Reset Pendiente en Stop Transaction")
                await self.perform_reset(self.pending_reset)  # Ejecutar el reset pendiente
                self.pending_reset = None  # Limpiar el reset pendiente
        
        except Exception as e:
            logger.error(f"Error al enviar mensaje StopTransaction: {e}")
        
  #Enviar MeterValues     
  # Revisar la documentacion, porque hay values que no reconoce como A, V en las unidades  y en el measured de la misma forma. 
    async def send_meter_values(self):
        while True:
            active_power = await self.lecturaMedidor()
            #self.meter=await self.lecturaMedidor()
            timestamp = datetime.utcnow().isoformat() + 'Z'

            request = call.MeterValues(
                connector_id=1,
                meter_value=[{
                    "timestamp": str(timestamp),
                    "sampled_value": [
                        {
                        "value": str(active_power),
                        "context": "Sample.Periodic",
                        "format": "Raw",
                        "measurand": "Energy.Active.Import.Register",
                        "location": "Outlet",
                        "unit": "Wh",
                        "phase": "L1"
                        }
                    ]
                }]
            )
            try:
                await self.call(request)
                logger.info(f"MeterValues sent: ActivePower={active_power}W")
            except Exception as e:
                logger.error(f"Failed to send MeterValues: {e}")
            await asyncio.sleep(60)
            
    #Enviar StatusNotification         
    async def send_status_notification(self, connector_id, status, error_code="NoError"):
            """
            Envia una notificacion de estado del cargador al servidor OCPP.
    
            :param connector_id: ID del conector que se notifica.
            :param status: Estado del punto de carga (ej. ChargePointStatus.Available).
            :param error_code: Codigo de error si hay problemas, 'NoError' si no hay error.
            """
            logger.info(f"Enviando StatusNotification: Conector ID={connector_id}, Estado={status}, Codigo Error={error_code}")
            
            # Crea la solicitud de statusNotification
            request = call.StatusNotification(
                connector_id=connector_id,
                error_code=error_code,
                status=status
            )
    
            try:
                response = await self.call(request)
                logger.info(f"Respuesta de StatusNotification: {response}")
            except Exception as e:
                logger.error(f"Error al enviar StatusNotification con estado '{status}' y codigo de error '{error_code}': {e}")



#################################################################################
##Recibidos OCPP####

    @on('TriggerMessage')
    async def on_trigger_message(self, requested_message):
        logger.info(f"Received TriggerMessage for {requested_message}")
        
        # Si esta esperando un TriggerMessage para BootNotification, continuar con el flujo
        if self.waiting_for_trigger_message and requested_message == 'BootNotification':
            logger.info("TriggerMessage received for BootNotification. Sending again...")
            self.waiting_for_trigger_message = False
            await self.send_boot_notification() 
            
            
            # Funcion ChangeAvailability
    @on('ChangeAvailability')
    async def on_change_availability(self, type, **kwargs):
        logger.info(f"ChangeAvailability recibido: type={type}")
        self.flagAvailability=True
        if self.sessionOn is False:  # Si no hay transaccion en curso, cambiar inmediatamente
            self.availability = "Inoperative" if type == "Inoperative" else "Operative"
            if type == "Inoperative":
                self.availability = "Inoperative"
                self.stateToMicro = await self.readFromUc(codigo=90) 
                asyncio.create_task(self.save_state(self.availability)) 
                logger.info("Guardar dato en JSON")
                logger.info("Estado de disponibilidad cambiado a Inoperativo")
                result = call_result.ChangeAvailability(status='Accepted')
                asyncio.create_task(self.display_inoperative_screen()) 
                return result
            elif type == "Operative":
                self.availability = "Operative"
                asyncio.create_task(self.save_state(self.availability))
                self.stateToMicro = await self.readFromUc(codigo=45)
                logger.info("Guardar dato en JSON")
                logger.info("Estado de disponibilidad cambiado a Operativo")
                result = call_result.ChangeAvailability(status='Accepted')
                # Iniciar la tarea pero no esperar a que termine
                asyncio.create_task(self.validate_rfid())  
                return result
                #return call_result.ChangeAvailability(status='Accepted')
        else:
            logger.info(f"ChangeAvailability programado para despues de la transaccion. Estado deseado: {type}")
            # Guardar el cambio de disponibilidad para despues de la transaccion
            self.pending_change_availability = type 
            return call_result.ChangeAvailability(status='Scheduled')
                    
    
        # Funcion RemoteStartTransaction
    @on('RemoteStartTransaction')
    async def listen_remote_start_transaction(self, **kwargs):
        logger.info("RemoteStartTransaction recibido. Iniciando transaccion sin validar RFID...")
        self.remoteStartTransaction=True
        if self.transaction_id is None:  # Si no hay transaccion en curso, cambiar inmediatamente
            # Intentar autorizar con el codigo RFID ingresado
            self.rfid_code=kwargs.get('id_tag', None)
            asyncio.create_task(self.send_remoteAuthorize_rfid(self.rfid_code))                
            return call_result.RemoteStartTransaction(
                status="Accepted"
            )       
     
         # Funcion RemoteStopTransaction   
    @on('RemoteStopTransaction')
    async def listen_remote_stop_transaction(self, **kwargs):
        logger.info("RemoteStopTransaction recibido. Finalizando transaccion...")
        asyncio.create_task(self.stop_transaction())                
        return call_result.RemoteStartTransaction(
            status="Accepted"
        ) 
################################################################################################################    
######################################FUNCION-RESET(RECIBE)#####################################################

        # Funcion Reset
    @on('Reset')
    async def listen_reset(self, type, **kwargs):
        logger.info(f"Reset recibido: tipo={type}")
        
        # Verificar si se puede realizar el reset inmediatamente
        if self.transaction_id is None:  # No hay transaccion en curso, se puede resetear
            await self.perform_reset(type)
            return call_result.Reset(status='Accepted')
        else:
            logger.info("Reset programado despues de la transaccion actual.")
            self.pending_reset = type  # Guardar el tipo de reset para despues de la transaccion
            return call_result.Reset(status='Accepted')
            
################################################################################################################
################################################################################################################         


    
    @on('SetChargingProfile')
    async def on_set_charging_profile(self, **kwargs):
        try:
            #print("Contenido de kwargs:", kwargs)  # Anade esta linea para depuracion
            profile = kwargs['cs_charging_profiles']
            connector_id = kwargs['connector_id']
            print(f"Conector: {connector_id}, Perfil: {profile}")
            # Almacenar el perfil en la estructura de datos
            if connector_id not in charging_profiles:
                charging_profiles[connector_id] = {}
            if profile['stack_level'] not in charging_profiles[connector_id]:
                charging_profiles[connector_id][profile['stack_level']] = []
    
            charging_profiles[connector_id][profile['stack_level']].append(profile)
            
            # Verificacion despues de almacenar el perfil
            print(f"Perfil almacenado: {charging_profiles[connector_id][profile['stack_level']]}")
    
            # Guardar en JSON para persistencia
            await self.save_charging_profiles()
            print("Perfiles de carga guardados correctamente.")
            # Asegurate de que el resultado se envie correctamente
            result = call_result.SetChargingProfile(status='Accepted')
            print("Respuesta enviada: ", result)
            return result

        except Exception as e:
            print(f"Error al procesar el perfil de carga: {e}")
            raise
    
    @on('ClearChargingProfile')
    async def on_clear_charging_profile(self, id=None, connectorId=None, chargingProfilePurpose=None, stackLevel=None):
        chargingProfileId = id  # Alias para mantener la coherencia en el codigo
        profiles_cleared = 0
    
        # Filtrar por connectorId
        if connectorId:
            connectors_to_clear = [connectorId] if connectorId in charging_profiles else []
        else:
            connectors_to_clear = list(charging_profiles.keys())
    
        for conn_id in connectors_to_clear:
            levels_to_clear = list(charging_profiles[conn_id].keys())  # Listado de niveles para iterar de forma segura
            for level in levels_to_clear:
                profiles_to_remove = [
                    profile for profile in charging_profiles[conn_id][level]
                    if ((chargingProfilePurpose is None or profile['chargingProfilePurpose'] == chargingProfilePurpose) and
                        (stackLevel is None or profile['stackLevel'] == stackLevel) and
                        (chargingProfileId is None or profile['charging_profile_id'] == chargingProfileId))
                ]
    
                # Eliminar perfiles coincidentes
                for profile in profiles_to_remove:
                    charging_profiles[conn_id][level].remove(profile)
                    profiles_cleared += 1
    
                # Eliminar el nivel si esta vacio
                if not charging_profiles[conn_id][level]:
                    del charging_profiles[conn_id][level]
    
            # Eliminar el conector si no tiene niveles restantes
            if not charging_profiles[conn_id]:
                del charging_profiles[conn_id]
    
        # Guardar los cambios en JSON
        await self.save_charging_profiles()
    
        # Confirmar la eliminacion
        if profiles_cleared > 0:
            return call_result.ClearChargingProfile(status='Accepted')
        else:
            return call_result.ClearChargingProfile(status='Unknown')

            
            
  ################################################################################################
  #####################################LOGICA DE ARRANQUE#########################################
  
  
    async def start1(self):
        #Ingresar estado del micro
        while True:
            #await self.load_charging_profiles()
            
            self.stateFromMicro=await self.readFromUc(codigo=self.readEstado)
            print("Estado recibido del micro: ", self.stateFromMicro)        
            
            if self.stateFromMicro==4:
                #pasa a 4.5
                #self.heartBeatAuthorization=True
                
                self.bootNotification=True
                self.startComm=True
                
                
                print("Estado recibido del micro: ", self.stateFromMicro)  
                logger.info("Comienza la comunicacion con la central OCPP.")
                break
                #outStart1 = True
                print(outStart1)
            elif self.stateFromMicro==9:
                logger.info("Estado 9: Falla detectada.")
                pass
            elif self.stateFromMicro == 5:
                pass    
            else:
                logger.info("Estado desconocido. Intente de nuevo.")
                pass
            await asyncio.sleep(1)
    
    async def ocppComm(self):
        print("aqui")
        
        while not self.startComm:
            await asyncio.sleep(1)
            ##print("qui")
        
        # Solo continuar si el estado es "4"
        logger.info("Inicio StartComm")
        
        if self.bootNotification:
            try:
                logger.info("Comienza el envio de BootNotification")
                boot_notification_success = await self.send_boot_notification()

                if boot_notification_success:
                    logger.info("BootNotification enviado correctamente")
                    # Marcar que ya no necesita reenviar el BootNotification
                    self.bootNotification = False

                elif self.waiting_for_trigger_message:
                    # Si esta esperando un TriggerMessage, pausamos el ciclo
                    logger.info("Esperando TriggerMessage antes de continuar...")
                    await asyncio.sleep(1)

            except Exception as e:
                logger.error(f"Error en la conexion: {e}")
                self.startComm = False  # Detener la comunicacion si hay un error importante

        else:
            # Si no hay BootNotification por enviar, duerme para evitar un ciclo frenetico
            await asyncio.sleep(1)
            
            
#################################RFID########################################################################
#############################################################################################################           

    # Funcion para simular la validacion RFID
    async def validate_rfid(self):
        while self.availability == "Operative":
            #i2c = busio.I2C(board.SCL, board.SDA)
            self.pn532.SAM_configuration()
            rfid_ref = True
            print("Waiting for validate RFID/NFC card...")
            while rfid_ref:
                uid = self.pn532.read_passive_target(timeout=0.5)
                await asyncio.sleep(0) 
                # Try again if no card is available.
                if uid is None:
                    await asyncio.sleep(0.1)  # Para evitar uso intensivo de CPU
                    continue    
                else:
                    entero = int.from_bytes(uid, byteorder='big')
                    print(entero)
                    rfid_code = str(entero)
                    self.rfid_code=rfid_code
                    rfid_ref = False

            #rfid_code = input("Ingrese el codigo RFID: ").strip()
            if rfid_code:
                await asyncio.sleep(2)
                if self.availability == "Inoperative":
                    break
                if self.remoteStartTransaction:
                    logger.info("remotestart=true")
                    break
                else:
                    logger.info("remotestart=false")
                # Intentar autorizar con el codigo RFID ingresado
                authorized = await self.send_authorize_rfid(rfid_code)     
                if authorized=="Accepted":  # Si la autorizacion es aceptada
                    self.stateToMicro = await self.readFromUc(codigo=5)
                    logger.info(f"RFID {rfid_code} aceptado. Estado: idle..Conecte el cargador..")
                     # Simular la conexion de la toma del cargador
                    self.sessionOn=True
                    await self.simulate_charger_connection() 
                    break  # Salir del loop despues de una validacion exitosa
                
                else:
                    if self.availability == "Inoperative":
                        break
                    self.stateToMicro = await self.readFromUc(codigo=46)
                    print(f"RFID {rfid_code} invalido, intente nuevamente.")
                    await asyncio.sleep(10)  # Esperar 5 segundos antes de volver a pedir la validacion RFID
                    self.stateToMicro = await self.readFromUc(codigo=45)
            
            else:
                print("Codigo RFID no valido. Ingrese un codigo valido.")
    
    
##################################################################################################################
#############################FUNCIONES PRINCIPALES CARGADOR#######################################################                
                
    # Funcion para mostrar la pantalla cuando esta inoperativa
    async def display_inoperative_screen(self):
        while self.availability2 == "Inoperative":
            logger.info("La estacion esta INOPERATIVA. No puede procesar operaciones en este momento.")
            await asyncio.sleep(5)  # Mantener la pantalla inoperativa visible
            if self.availability2 == "Operative":
                break
                
             

    # Simular conexion del cargador
    async def simulate_charger_connection(self):
        start_time = datetime.utcnow()  # Marca el tiempo de inicio de la espera de conexion
        while True:
            await asyncio.sleep(3)  # Espera antes de volver a preguntar  
            
            charger_input = await self.readFromUc(codigo=self.readEstado)
            print("Estado recibido del micro: ", charger_input)
            
            # Si se recibe un estado 6 o 7, se considera conectado y se inicia la transaccion
            if charger_input == 7:
                logger.info("Cargador conectado. Estado: conectado")
                self.sessionOn = True
                await asyncio.sleep(1)  # Espera antes de iniciar la transaccion
                await self.start_transaction()  # Iniciar la transaccion OCPP
                await self.ask_to_stop_transaction()  # Preguntar si detener la transaccion
                #self.conectorConnected=True
                break  # Rompe el ciclo si el estado es 6 o 7
            
            # Comprueba si han pasado 60 segundos sin recibir un estado 6 o 7
            elapsed_time = (datetime.utcnow() - start_time).total_seconds()
            if elapsed_time >= 60:
                logger.warning("No se ha detectado una conexion en 60 segundos. Terminando la sesion.")
                self.rfid_code=None 
                logger.info("Regresando al estado de validacion de RFID.")
                self.stateToMicro = await self.readFromUc(codigo=45)
                #self.stateToMicro = await self.readFromUc(codigo=99)
                await asyncio.sleep(2)
                #await self.send_status_notification(connector_id=1, status="Available")
                asyncio.create_task(self.validate_rfid())              
                #await self.stop_transaction()  # Llama a stop_transaction si no se detecta conexion en 60 segundos
                break
            else:
                logger.warning("No se ha conectado el auto")
    


            
    # Preguntar si el usuario desea detener la transaccion
    async def ask_to_stop_transaction(self):
        logger.info("Inicio ask to stop")
        if self.transaction_id is not None:
            logger.info("despues del if ask to stop")
            while self.availability == "Operative" and self.transaction_id is not None:
                logger.info("despues del primer while ask to stop")
                #i2c = busio.I2C(board.SCL, board.SDA)
                self.pn532.SAM_configuration()
                rfid_ref = True
                print("Waiting for RFID/NFC card, para terminar transaccion")
                while rfid_ref:
                    logger.info("despues del segundo while ask to stop")
                    self.stateFromMicro=await self.readFromUc(codigo=self.readEstado)
                    print("Estado recibido del micro: ", self.stateFromMicro)
                    #if self.stateFromMicro==10:
                    #await asyncio.sleep(5)
                    if self.stateFromMicro==5 or self.stateFromMicro==6:
                        self.conect_disconect="Disconect"
                        logger.info("desconectado a la mala")
                        #await asyncio.sleep(1)
                        rfid_ref = False
                        self.stateToMicro = await self.readFromUc(codigo=13)
                        await asyncio.sleep(0.1)
                        self.stateToMicro = await self.readFromUc(codigo=13)
                        await self.stop_transaction()
                        break
                        #pasa a 4.5
                        #self.heartBeatAuthorization=True
                    
                    uid = self.pn532.read_passive_target(timeout=0.5)
                    await asyncio.sleep(0) 
                    # Try again if no card is available.
                    if uid is None:
                        await asyncio.sleep(0.1)  # Para evitar uso intensivo de CPU
                        continue    
                    else:
                        entero = int.from_bytes(uid, byteorder='big')
                        print(entero)
                        rfid_code = str(entero)
                        if rfid_code==self.rfid_code:
                            rfid_ref = False
                            await self.stop_transaction()
                            break
            

    # Nueva funcion para mostrar un resumen de la sesion
    async def Resume(self):
        logger.info("Mostrando el resumen de la sesion.")
        print("resumen:...")
        await asyncio.sleep(10)  # Mostrar el resumen durante 10 segundos
        if self.pending_change_availability=="Inoperative":
            logger.info("Inhabilitando...")
            self.availability=="Inoperative"
            asyncio.create_task(self.save_state(self.availability)) 
            #save_state(self.availability)  # Guardar el estado modificado
            logger.info("Guardar dato en JSON")
            self.pending_change_availability==None
            await self.display_inoperative_screen()
            self.stateToMicro = await self.readFromUc(codigo=90)
        else:
            logger.info("Regresando al estado de validacion de RFID.")
            self.stateToMicro = await self.readFromUc(codigo=45)
            #self.stateToMicro = await self.readFromUc(codigo=99)
            await asyncio.sleep(2)
            #await self.send_status_notification(connector_id=1, status="Available")
            asyncio.create_task(self.validate_rfid())
            #await self.perform_reset("Hard")
    
    # Nueva funcion para mostrar un resumen de la sesion
    async def listenState(self):
        while True: 
              await asyncio.sleep(1)  # Mostrar el resumen durante 10 segundos
              self.stateFromMicro=await self.readFromUc(codigo=self.readEstado)
              print("Estado recibido del micro: ", self.stateFromMicro)
              if self.stateFromMicro==9:
                  await self.send_status_notification(connector_id=1, status="Faulted")
              elif self.stateFromMicro==45:
                  await self.send_status_notification(connector_id=1, status="Available")
              elif self.stateFromMicro==5:
                  await self.send_status_notification(connector_id=1, status="Preparing")
              elif self.stateFromMicro==7:
                  await self.send_status_notification(connector_id=1, status="Charging")
              elif self.stateFromMicro==13:
                  await self.send_status_notification(connector_id=1, status="Finishing")
              elif self.stateFromMicro==14:
                  await self.send_status_notification(connector_id=1, status="Finishing")
              elif self.stateFromMicro==90:
                  await self.send_status_notification(connector_id=1, status="Unavailable")

#################################RESET_ENVIA_MICRO##################################################
####################################################################################################     

           
    # Funcion para reset  
    async def perform_reset(self, type):
        """ Ejecutar el reseteo del sistema """
        if type == "Hard":
            logger.info("Ejecutando reseteo HARD...")
            # Agregar logica para el reset Hard aqui (por ejemplo, reiniciar el sistema)
            #sys.exit()  # Finaliza el programa simulando un reinicio de hardware
            self.stateToMicro = await self.readFromUc(codigo=99)
            await asyncio.sleep(2)
            os.system("sudo reboot")
        elif type == "Soft":
            logger.info("Ejecutando reseteo SOFT...")
            # Agregar logica para el reset Soft aqui (por ejemplo, reestablecer variables)
            await self.restart_soft()
            
    # Fucnion para soft Reset
    async def restart_soft(self):
        """ Logica de reinicio SOFT """
        # Reiniciar variables y estado del sistema
        await self.stop_transaction()  # Llamar a la funcion stopTransaction
        logger.info("Sistema reiniciado (Soft Reset).")
        self.stateToMicro = await self.readFromUc(codigo=99)
        await asyncio.sleep(2)
        os.system("sudo reboot")

 
####################################################################################################
####################################################################################################   

   
                
    # Funcion para habilitar hilos principales
    async def enable_main_functions(self):
        # Verifica si BootNotification fue aceptado y Availability esta disponible
        print(f"Estado..probando:", self.availability2)  # Imprimir el estado inicial
        
        if self.availability2 == "Inoperative":
            logger.warning("No se cumplen las condiciones para iniciar funciones principales.")
            asyncio.create_task(self.send_heartbeat())
            asyncio.create_task(self.send_meter_values())
            asyncio.create_task(self.listenState())
            #await self.display_inoperative_screen()  # Mostrar pantalla de inoperativo
            return
        else:
            logger.info("BootNotification aceptado y Charge Point disponible. Iniciando funciones principales...")
            
            # Habilitar las tareas en segundo plano
            asyncio.create_task(self.send_heartbeat())
            asyncio.create_task(self.send_meter_values())
            asyncio.create_task(self.monitor_and_apply_profiles(0))
            #asyncio.create_task(self.listenState())
            #asyncio.create_task(self.validate_rfid())  # Simula la validacion de RFID



    
##################################################################################################################
#############################FUNCIONES SECUNDARIAS CARGADOR#######################################################     


    def ajustar_reloj(current_time):
        try:
            # Convertir la cadena de tiempo en un objeto datetime
            nuevo_tiempo = datetime.fromisoformat(current_time.replace("Z", "+00:00"))
            # Formatear en el formato necesario para el comando 'date'
            comando = f'sudo date -s "{nuevo_tiempo.strftime("%Y-%m-%d %H:%M:%S")}"'
            os.system(comando)
            logger.info(f"Reloj ajustado a: {nuevo_tiempo}")
        except Exception as e:
            logger.error(f"Error ajustando el reloj: {e}")
            

    # Funcion para guardar el estado en el archivo JSON
    async def save_state(self, status):
        with open(JSON_PATH, 'w') as file:
            json.dump({"status": status}, file)
        
    # Funcion para cargar el estado desde el archivo JSON
    async def load_state(self):
        if os.path.exists(JSON_PATH):
            with open(JSON_PATH, 'r') as file:
                data = json.load(file)
                return data.get("status", "Available")  # Estado por defecto si no se encuentra "status"
        else:
            return "Available"  # Estado por defecto si el archivo no existe
    
        # Funciones para persistencia en JSON
    async def load_charging_profiles(self):
        """ Cargar perfiles de carga desde un archivo JSON al iniciar el script. """
        if os.path.exists("charging_profiles.json"):
            with open("charging_profiles.json", "r") as file:
                data = json.load(file)
                for connector_id, profiles in data.items():
                    for stack_level, profile_list in profiles.items():
                        charging_profiles[int(connector_id)][int(stack_level)] = profile_list
    
    
    async def save_charging_profiles(self):
        try:
            # Guardar en JSON con un convertidor personalizado para Decimal
            with open("charging_profiles.json", "w") as file:
                json.dump(charging_profiles, file, default=decimal_default)
            print("Perfiles de carga guardados correctamente.")
        except Exception as e:
            print(f"Error al guardar los perfiles de carga: {e}")
            
                

    async def apply_charging_profile(self, connector_id):
        # Asegurarse de que 'now' no tiene informacion de zona horaria (offset-naive)
        now = datetime.utcnow().replace(tzinfo=None)
        active_limit = None
    
        logger.info(f"Hora actual UTC: {now.isoformat()}")
        
        # Obtener el perfil activo con la mayor prioridad (stack level mas alto)
        for stack_level in sorted(charging_profiles[connector_id].keys(), reverse=True):
            logger.info(f"Revisando stack_level: {stack_level} para conector {connector_id}")
            
            for profile in charging_profiles[connector_id][stack_level]:
                schedule = profile['charging_schedule']
                # Convertir start_schedule a naive
                start_schedule = datetime.fromisoformat(schedule['start_schedule'].replace("Z", "+00:00")).replace(tzinfo=None)
                duration = schedule.get('duration', None)
                end_schedule = start_schedule + timedelta(seconds=duration) if duration else None
    
                logger.info(f"Perfil encontrado: Inicio = {start_schedule.isoformat()}, Duracion = {duration}s, Fin = {end_schedule.isoformat() if end_schedule else 'Indefinido'}")
    
                # Verificar si el perfil esta activo en este momento
                if start_schedule <= now <= (end_schedule if end_schedule else now):
                    logger.info("El perfil esta activo en el horario actual.")
    
                    # Recorre los periodos de carga definidos en charging_schedule_period
                    for period in schedule['charging_schedule_period']:
                        period_start = period['start_period']  # En segundos desde el inicio del perfil
                        limit = period['limit']  # Limite de corriente para este periodo
                        #period_start_time = start_schedule + timedelta(seconds=period_start)
                        period_start_time = start_schedule
    
                        logger.info(f"Revisando periodo: inicio a {period_start_time.isoformat()}, limite = {limit} A")
    
                        # Verificar si el tiempo actual esta dentro del periodo
                        if period_start_time <= now < (period_start_time + timedelta(seconds=duration)):
                            active_limit = limit  # Establecer el limite activo
                            logger.info(f"Limite de corriente aplicable encontrado: {active_limit} A para el conector {connector_id}")
                            break  # Salir si encontramos un limite activo
    
                else:
                    logger.info("El perfil no esta activo en el horario actual.")
    
                if active_limit is not None:
                    break  # Detenerse en el perfil activo con mayor prioridad (mas alto stack_level)
    
        # Aplicar el limite de corriente encontrado
        if active_limit:
                # Sumamos 100 al limite activo y lo enviamos como codigo a la funcion readFromUc
            codigo1 = active_limit + 100
            logger.info(f"Aplicando limite de carga de {active_limit} A (codigo={codigo1}) para el conector {connector_id}")
            self.stateToMicro = await self.readFromUc(codigo=codigo1)

    
    async def monitor_and_apply_profiles(self, connector_id):
        """
        Monitorea y aplica el perfil activo en intervalos regulares.
        """
        while True:
            logger.info("Probando charging profile")
            await self.apply_charging_profile(connector_id)
            await asyncio.sleep(10)  # Evaluar y aplicar cada 10 segundos

###############################################################################
############################### Comm I2C ##################################################
    async def readFromUc(self, verbose = False, codigo = 0):
        print("entre al i2c con:" , codigo)
        await self.writeI2C(codigo)
        await self.readI2C()
        response = int(self.bufferRead[0])
        return response
    
    async def writeI2C(self, queryCode):
        print("entre al writei2c")
        await asyncio.to_thread(self.i2c.writeto, self.slave_address_I2c, bytes([queryCode]))
    
        
    async def readI2C(self):
        print("entre al readi2c")
        await asyncio.to_thread(self.i2c.readfrom_into, self.slave_address_I2c, self.bufferRead)

    async def lecturaMedidor(self):
        print("entre a lectura medidor")
        response = await asyncio.to_thread(self.instrument.read_float, registeraddress=0x4000, functioncode=4)#voltage
        return response


async def main():
    # Cargar parametros desde el archivo JSON
    try:
        with open("config.json", "r") as file:
            config_data = json.load(file)
            url_server = config_data.get("URLserver")
            if not url_server:
                logger.error("No se encontro 'URLserver' en el archivo JSON.")
                return
    except FileNotFoundError:
        logger.error("Archivo de configuracion JSON no encontrado.")
        return
    except json.JSONDecodeError:
        logger.error("Error al decodificar el archivo JSON.")
        return
    try:
        # Intento de conexion al WebSocket
        async with websockets.connect(
            #"ws://192.168.0.107:8180/steve/websocket/CentralSystemService/3",
            url_server,
            subprotocols=["ocpp1.6"]
        ) as ws:
            # Instancia de ChargePoint
            cp = ChargePoint("CP_1", ws)
            
            # Cargar perfiles de carga desde el archivo JSON
            await cp.load_charging_profiles()

            tasks = [
                asyncio.create_task(cp.start()),
                asyncio.create_task(cp.start1()),
                asyncio.create_task(cp.ocppComm()),
                #asyncio.create_task(cp.listenState()),
                #asyncio.create_task(cp.send_boot_notification()),
                #asyncio.create_task(cp.EV2()),
                #asyncio.create_task(cp.EV2_status_comm())
                #asyncio.create_task(cp.EV2_status_measurenment())            
            ]

            # Espera a que todas las tareas terminen
            await asyncio.gather(*tasks, return_exceptions=True)
            
            logger.info("Disconnecting from server")
            await ws.close()

    except OSError as e:
        # Manejo de error de conexion sin bloquear la ejecucion
        logger.error(f"No se pudo conectar al servidor WebSocket: {e}")
        # Puedes definir acciones alternativas aqui si es necesario
        # Ejemplo: Reintentar despues de un tiempo
        await asyncio.sleep(5)  # Espera antes de reintentar
        await main()  # Reintentar la conexion

if __name__ == "__main__":
    asyncio.run(main())