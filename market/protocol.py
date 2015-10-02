__author__ = 'chris'

import json
import nacl.signing
import nacl.utils
import nacl.encoding
import nacl.hash
from nacl.public import PrivateKey, PublicKey, Box
from zope.interface import implements
from rpcudp import RPCProtocol
from interfaces import MessageProcessor
from log import Logger
from protos.message import GET_CONTRACT, GET_IMAGE, GET_PROFILE, GET_LISTINGS, \
    GET_USER_METADATA, FOLLOW, UNFOLLOW, GET_FOLLOWERS, GET_FOLLOWING, NOTIFY, \
    GET_CONTRACT_METADATA, MESSAGE, ORDER, ORDER_CONFIRMATION, COMPLETE_ORDER
from market.contracts import Contract
from market.profile import Profile
from protos.objects import Metadata, Listings, Followers, Plaintext_Message
from binascii import hexlify
from zope.interface.verify import verifyObject
from zope.interface.exceptions import DoesNotImplement
from interfaces import NotificationListener, MessageListener
from collections import OrderedDict
from keyutils.bip32utils import derive_childkey

class MarketProtocol(RPCProtocol):
    implements(MessageProcessor)

    def __init__(self, node_proto, router, signing_key, database):
        self.router = router
        RPCProtocol.__init__(self, node_proto, router)
        self.log = Logger(system=self)
        self.multiplexer = None
        self.db = database
        self.signing_key = signing_key
        self.listeners = []
        self.handled_commands = [GET_CONTRACT, GET_IMAGE, GET_PROFILE, GET_LISTINGS, GET_USER_METADATA,
                                 GET_CONTRACT_METADATA, FOLLOW, UNFOLLOW, GET_FOLLOWERS, GET_FOLLOWING,
                                 NOTIFY, MESSAGE, ORDER, ORDER_CONFIRMATION, COMPLETE_ORDER]

    def connect_multiplexer(self, multiplexer):
        self.multiplexer = multiplexer

    def add_listener(self, listener):
        self.listeners.append(listener)

    def rpc_get_contract(self, sender, contract_hash):
        self.log.info("Looking up contract ID %s" % contract_hash.encode('hex'))
        self.router.addContact(sender)
        try:
            with open(self.db.HashMap().get_file(contract_hash), "r") as filename:
                contract = filename.read()
            return [contract]
        except Exception:
            self.log.warning("Could not find contract %s" % contract_hash.encode('hex'))
            return ["None"]

    def rpc_get_image(self, sender, image_hash):
        self.log.info("Looking up image with hash %s" % image_hash.encode('hex'))
        self.router.addContact(sender)
        try:
            with open(self.db.HashMap().get_file(image_hash), "r") as filename:
                image = filename.read()
            return [image]
        except Exception:
            self.log.warning("Could not find image %s" % image_hash.encode('hex'))
            return ["None"]

    def rpc_get_profile(self, sender):
        self.log.info("Fetching profile")
        self.router.addContact(sender)
        try:
            proto = Profile(self.db).get(True)
            return [proto, self.signing_key.sign(proto)[:64]]
        except Exception:
            self.log.error("Unable to load the profile")
            return ["None"]

    def rpc_get_user_metadata(self, sender):
        self.log.info("Fetching metadata")
        self.router.addContact(sender)
        try:
            proto = Profile(self.db).get(False)
            m = Metadata()
            m.name = proto.name
            m.handle = proto.handle
            m.short_description = proto.short_description
            m.avatar_hash = proto.avatar_hash
            m.nsfw = proto.nsfw
            return [m.SerializeToString(), self.signing_key.sign(m.SerializeToString())[:64]]
        except Exception:
            self.log.error("Unable to get the profile metadata")
            return ["None"]

    def rpc_get_listings(self, sender):
        self.log.info("Fetching listings")
        self.router.addContact(sender)
        try:
            p = Profile(self.db).get()
            l = Listings()
            l.ParseFromString(self.db.ListingsStore().get_proto())
            l.handle = p.handle
            l.avatar_hash = p.avatar_hash
            return [l.SerializeToString(), self.signing_key.sign(l.SerializeToString())[:64]]
        except Exception:
            self.log.warning("Could not find any listings in the database")
            return ["None"]

    def rpc_get_contract_metadata(self, sender, contract_hash):
        self.log.info("Fetching metadata for contract %s" % hexlify(contract_hash))
        self.router.addContact(sender)
        try:
            proto = self.db.ListingsStore().get_proto()
            l = Listings()
            l.ParseFromString(proto)
            for listing in l.listing:
                if listing.contract_hash == contract_hash:
                    ser = listing.SerializeToString()
            return [ser, self.signing_key.sign(ser)[:64]]
        except Exception:
            self.log.warning("Could not find metadata for contract %s" % hexlify(contract_hash))
            return ["None"]

    def rpc_follow(self, sender, proto, signature):
        self.log.info("Follow request from %s" % sender.id.encode("hex"))
        self.router.addContact(sender)
        try:
            verify_key = nacl.signing.VerifyKey(sender.signed_pubkey[64:])
            verify_key.verify(proto, signature)
            f = Followers.Follower()
            f.ParseFromString(proto)
            if f.guid != sender.id:
                raise Exception('GUID does not match sending node')
            if f.following != self.proto.guid:
                raise Exception('Following wrong node')
            f.signature = signature
            self.db.FollowData().set_follower(f)
            proto = Profile(self.db).get(False)
            m = Metadata()
            m.name = proto.name
            m.handle = proto.handle
            m.avatar_hash = proto.avatar_hash
            m.nsfw = proto.nsfw
            return ["True", m.SerializeToString(), self.signing_key.sign(m.SerializeToString())[:64]]
        except Exception:
            self.log.warning("Failed to validate follower")
            return ["False"]

    def rpc_unfollow(self, sender, signature):
        self.log.info("Unfollow request from %s" % sender.id.encode("hex"))
        self.router.addContact(sender)
        try:
            verify_key = nacl.signing.VerifyKey(sender.signed_pubkey[64:])
            verify_key.verify("unfollow:" + self.proto.guid, signature)
            f = self.db.FollowData()
            f.delete_follower(sender.id)
            return ["True"]
        except Exception:
            self.log.warning("Failed to validate follower signature")
            return ["False"]

    def rpc_get_followers(self, sender):
        self.log.info("Fetching followers list from db")
        self.router.addContact(sender)
        ser = self.db.FollowData().get_followers()
        if ser is None:
            return ["None"]
        else:
            return [ser, self.signing_key.sign(ser)[:64]]

    def rpc_get_following(self, sender):
        self.log.info("Fetching following list from db")
        self.router.addContact(sender)
        ser = self.db.FollowData().get_following()
        if ser is None:
            return ["None"]
        else:
            return [ser, self.signing_key.sign(ser)[:64]]

    def rpc_notify(self, sender, message, signature):
        if len(message) <= 140 and self.db.FollowData().is_following(sender.id):
            try:
                verify_key = nacl.signing.VerifyKey(sender.signed_pubkey[64:])
                verify_key.verify(message, signature)
            except Exception:
                return ["False"]
            self.log.info("Received a notification from %s" % sender)
            self.router.addContact(sender)
            for listener in self.listeners:
                try:
                    verifyObject(NotificationListener, listener)
                    listener.notify(sender.id, message)
                except DoesNotImplement:
                    pass
            return ["True"]
        else:
            return ["False"]

    def rpc_message(self, sender, pubkey, encrypted):
        try:
            box = Box(PrivateKey(self.signing_key.encode(nacl.encoding.RawEncoder)), PublicKey(pubkey))
            plaintext = box.decrypt(encrypted)
            p = Plaintext_Message()
            p.ParseFromString(plaintext)
            signature = p.signature
            p.ClearField("signature")
            verify_key = nacl.signing.VerifyKey(p.signed_pubkey[64:])
            verify_key.verify(p.SerializeToString(), signature)
            h = nacl.hash.sha512(p.signed_pubkey)
            pow_hash = h[64:128]
            if int(pow_hash[:6], 16) >= 50 or hexlify(p.sender_guid) != h[:40] or p.sender_guid != sender.id:
                raise Exception('Invalid guid')
            self.log.info("Received a message from %s" % sender)
            self.router.addContact(sender)
            for listener in self.listeners:
                try:
                    verifyObject(MessageListener, listener)
                    listener.notify(p, signature)
                except DoesNotImplement:
                    pass
            return ["True"]
        except Exception:
            self.log.error("Received invalid message from %s" % sender)
            return ["False"]

    def rpc_order(self, sender, pubkey, encrypted):
        try:
            box = Box(PrivateKey(self.signing_key.encode(nacl.encoding.RawEncoder)), PublicKey(pubkey))
            order = box.decrypt(encrypted)
            c = Contract(self.db, contract=json.loads(order, object_pairs_hook=OrderedDict),
                         testnet=self.multiplexer.testnet)
            if c.verify(sender.signed_pubkey[64:]):
                self.router.addContact(sender)
                self.log.info("Received an order from %s" % sender)
                payment_address = c.contract["buyer_order"]["order"]["payment"]["address"]
                chaincode = self.contract["buyer_order"]["order"]["payment"]["chaincode"]
                masterkey_b = self.contract["buyer_order"]["order"]["id"]["pubkeys"]["bitcoin"]
                buyer_key = derive_childkey(masterkey_b, chaincode)
                amount = self.contract["buyer_order"]["order"]["payment"]["amount"]
                listing_hash = self.contract["buyer_order"]["order"]["ref_hash"]
                signature = self.signing_key.sign(
                    str(payment_address) + str(amount) + str(listing_hash) + str(buyer_key))[:64]
                c.await_funding(self.multiplexer.ws, self.multiplexer.blockchain, signature, False)
                return [signature]
            else:
                self.log.error("Received invalid order from %s" % sender)
                return ["False"]
        except Exception:
            self.log.error("Unable to decrypt order from %s" % sender)
            return ["False"]

    def rpc_order_confirmation(self, sender, pubkey, encrypted):
        try:
            box = Box(PrivateKey(self.signing_key.encode(nacl.encoding.RawEncoder)), PublicKey(pubkey))
            order = box.decrypt(encrypted)
            c = Contract(self.db, contract=json.loads(order, object_pairs_hook=OrderedDict),
                         testnet=self.multiplexer.testnet)
            contract_id = c.accept_order_confirmation(self.multiplexer.ws)
            if contract_id:
                self.router.addContact(sender)
                self.log.info("Received confirmation for order %s" % contract_id)
                return ["True"]
            else:
                self.log.error("Received invalid order confirmation from %s" % sender)
                return ["False"]
        except Exception:
            self.log.error("Unable to decrypt order confirmation from %s" % sender)
            return ["False"]

    def rpc_complete_order(self, sender, pubkey, encrypted):
        try:
            box = Box(PrivateKey(self.signing_key.encode(nacl.encoding.RawEncoder)), PublicKey(pubkey))
            order = box.decrypt(encrypted)
            c = Contract(self.db, contract=json.loads(order, object_pairs_hook=OrderedDict),
                         testnet=self.multiplexer.testnet)

            # FIXME: this is where I left off
            contract_id = c.accept_order_confirmation(self.multiplexer.ws)
            if contract_id:
                self.router.addContact(sender)
                self.log.info("Received confirmation for order %s" % contract_id)
                return ["True"]
            else:
                self.log.error("Received invalid order confirmation from %s" % sender)
                return ["False"]
        except Exception:
            self.log.error("Unable to decrypt order confirmation from %s" % sender)
            return ["False"]

    def callGetContract(self, nodeToAsk, contract_hash):
        address = (nodeToAsk.ip, nodeToAsk.port)
        d = self.get_contract(address, contract_hash)
        return d.addCallback(self.handleCallResponse, nodeToAsk)

    def callGetImage(self, nodeToAsk, image_hash):
        address = (nodeToAsk.ip, nodeToAsk.port)
        d = self.get_image(address, image_hash)
        return d.addCallback(self.handleCallResponse, nodeToAsk)

    def callGetProfile(self, nodeToAsk):
        address = (nodeToAsk.ip, nodeToAsk.port)
        d = self.get_profile(address)
        return d.addCallback(self.handleCallResponse, nodeToAsk)

    def callGetUserMetadata(self, nodeToAsk):
        address = (nodeToAsk.ip, nodeToAsk.port)
        d = self.get_user_metadata(address)
        return d.addCallback(self.handleCallResponse, nodeToAsk)

    def callGetListings(self, nodeToAsk):
        address = (nodeToAsk.ip, nodeToAsk.port)
        d = self.get_listings(address)
        return d.addCallback(self.handleCallResponse, nodeToAsk)

    def callGetContractMetadata(self, nodeToAsk, contract_hash):
        address = (nodeToAsk.ip, nodeToAsk.port)
        d = self.get_contract_metadata(address, contract_hash)
        return d.addCallback(self.handleCallResponse, nodeToAsk)

    def callFollow(self, nodeToAsk, proto, signature):
        address = (nodeToAsk.ip, nodeToAsk.port)
        d = self.follow(address, proto, signature)
        return d.addCallback(self.handleCallResponse, nodeToAsk)

    def callUnfollow(self, nodeToAsk, signature):
        address = (nodeToAsk.ip, nodeToAsk.port)
        d = self.unfollow(address, signature)
        return d.addCallback(self.handleCallResponse, nodeToAsk)

    def callGetFollowers(self, nodeToAsk):
        address = (nodeToAsk.ip, nodeToAsk.port)
        d = self.get_followers(address)
        return d.addCallback(self.handleCallResponse, nodeToAsk)

    def callGetFollowing(self, nodeToAsk):
        address = (nodeToAsk.ip, nodeToAsk.port)
        d = self.get_following(address)
        return d.addCallback(self.handleCallResponse, nodeToAsk)

    def callNotify(self, nodeToAsk, message, signature):
        address = (nodeToAsk.ip, nodeToAsk.port)
        d = self.notify(address, message, signature)
        return d.addCallback(self.handleCallResponse, nodeToAsk)

    def callMessage(self, nodeToAsk, ehemeral_pubkey, ciphertext):
        address = (nodeToAsk.ip, nodeToAsk.port)
        d = self.message(address, ehemeral_pubkey, ciphertext)
        return d.addCallback(self.handleCallResponse, nodeToAsk)

    def callOrder(self, nodeToAsk, ephem_pubkey, encrypted_contract):
        address = (nodeToAsk.ip, nodeToAsk.port)
        d = self.order(address, ephem_pubkey, encrypted_contract)
        return d.addCallback(self.handleCallResponse, nodeToAsk)

    def callOrderConfirmation(self, nodeToAsk, ephem_pubkey, encrypted_contract):
        address = (nodeToAsk.ip, nodeToAsk.port)
        d = self.order_confirmation(address, ephem_pubkey, encrypted_contract)
        return d.addCallback(self.handleCallResponse, nodeToAsk)

    def callCompleteOrder(self, nodeToAsk, ephem_pubkey, encrypted_contract):
        address = (nodeToAsk.ip, nodeToAsk.port)
        d = self.complete_order(address, ephem_pubkey, encrypted_contract)
        return d.addCallback(self.handleCallResponse, nodeToAsk)

    def handleCallResponse(self, result, node):
        """
        If we get a response, add the node to the routing table.  If
        we get no response, make sure it's removed from the routing table.
        """
        if result[0]:
            self.log.info("got response from %s, adding to router" % node)
            self.router.addContact(node)
        else:
            self.log.debug("no response from %s, removing from router" % node)
            self.router.removeContact(node)
        return result

    def __iter__(self):
        return iter(self.handled_commands)
