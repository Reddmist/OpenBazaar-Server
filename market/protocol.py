__author__ = 'chris'

import bitcointools
import json
import os
import pickle
import nacl.signing
import nacl.utils
import nacl.encoding
import nacl.hash
from binascii import unhexlify
from collections import OrderedDict
from config import DATA_FOLDER
from interfaces import MessageProcessor, BroadcastListener, MessageListener, NotificationListener
from keys.bip32utils import derive_childkey
from keys.keychain import KeyChain
from log import Logger
from market.contracts import Contract
from market.moderation import process_dispute, close_dispute
from market.profile import Profile
from market.transactions import BitcoinTransaction
from nacl.public import PublicKey, Box
from net.rpcudp import RPCProtocol
from protos.message import GET_CONTRACT, GET_IMAGE, GET_PROFILE, GET_LISTINGS, GET_USER_METADATA,\
    GET_CONTRACT_METADATA, FOLLOW, UNFOLLOW, GET_FOLLOWERS, GET_FOLLOWING, BROADCAST, MESSAGE, ORDER, \
    ORDER_CONFIRMATION, COMPLETE_ORDER, DISPUTE_OPEN, DISPUTE_CLOSE, GET_RATINGS, REFUND
from protos.objects import Metadata, Listings, Followers, PlaintextMessage
from zope.interface import implements
from zope.interface.exceptions import DoesNotImplement
from zope.interface.verify import verifyObject


class MarketProtocol(RPCProtocol):
    implements(MessageProcessor)

    def __init__(self, node, router, signing_key, database):
        self.router = router
        self.node = node
        RPCProtocol.__init__(self, node, router)
        self.log = Logger(system=self)
        self.multiplexer = None
        self.db = database
        self.signing_key = signing_key
        self.listeners = []
        self.handled_commands = [GET_CONTRACT, GET_IMAGE, GET_PROFILE, GET_LISTINGS, GET_USER_METADATA,
                                 GET_CONTRACT_METADATA, FOLLOW, UNFOLLOW, GET_FOLLOWERS, GET_FOLLOWING,
                                 BROADCAST, MESSAGE, ORDER, ORDER_CONFIRMATION, COMPLETE_ORDER, DISPUTE_OPEN,
                                 DISPUTE_CLOSE, GET_RATINGS, REFUND]

    def connect_multiplexer(self, multiplexer):
        self.multiplexer = multiplexer

    def add_listener(self, listener):
        self.listeners.append(listener)

    def rpc_get_contract(self, sender, contract_hash):
        self.log.info("serving contract %s to %s" % (contract_hash.encode('hex'), sender))
        self.router.addContact(sender)
        try:
            with open(self.db.filemap.get_file(contract_hash.encode("hex")), "r") as filename:
                contract = filename.read()
            return [contract]
        except Exception:
            self.log.warning("could not find contract %s" % contract_hash.encode('hex'))
            return None

    def rpc_get_image(self, sender, image_hash):
        self.router.addContact(sender)
        try:
            if len(image_hash) != 20:
                self.log.warning("Image hash is not 20 characters %s" % image_hash)
                raise Exception("Invalid image hash")
            self.log.info("serving image %s to %s" % (image_hash.encode('hex'), sender))
            with open(self.db.filemap.get_file(image_hash.encode("hex")), "rb") as filename:
                image = filename.read()
            return [image]
        except Exception:
            self.log.warning("could not find image %s" % image_hash[:20].encode('hex'))
            return None

    def rpc_get_profile(self, sender):
        self.log.info("serving profile to %s" % sender)
        self.router.addContact(sender)
        try:
            proto = Profile(self.db).get(True)
            return [proto, self.signing_key.sign(proto)[:64]]
        except Exception:
            self.log.error("unable to load the profile")
            return None

    def rpc_get_user_metadata(self, sender):
        self.log.info("serving user metadata to %s" % sender)
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
            self.log.error("unable to load profile metadata")
            return None

    def rpc_get_listings(self, sender):
        self.log.info("serving store listings to %s" % sender)
        self.router.addContact(sender)
        try:
            p = Profile(self.db).get()
            l = Listings()
            l.ParseFromString(self.db.listings.get_proto())
            l.handle = p.handle
            l.avatar_hash = p.avatar_hash
            return [l.SerializeToString(), self.signing_key.sign(l.SerializeToString())[:64]]
        except Exception:
            self.log.warning("could not find any listings in the database")
            return None

    def rpc_get_contract_metadata(self, sender, contract_hash):
        self.log.info("serving metadata for contract %s to %s" % (contract_hash.encode("hex"), sender))
        self.router.addContact(sender)
        try:
            proto = self.db.listings.get_proto()
            p = Profile(self.db).get()
            l = Listings()
            l.ParseFromString(proto)
            for listing in l.listing:
                if listing.contract_hash == contract_hash:
                    listing.avatar_hash = p.avatar_hash
                    listing.handle = p.handle
                    ser = listing.SerializeToString()
            return [ser, self.signing_key.sign(ser)[:64]]
        except Exception:
            self.log.warning("could not find metadata for contract %s" % contract_hash.encode("hex"))
            return None

    def rpc_follow(self, sender, proto, signature):
        self.log.info("received follow request from %s" % sender)
        self.router.addContact(sender)
        try:
            verify_key = nacl.signing.VerifyKey(sender.pubkey)
            verify_key.verify(proto, signature)
            f = Followers.Follower()
            f.ParseFromString(proto)
            if f.guid != sender.id:
                raise Exception('GUID does not match sending node')
            if f.following != self.node.id:
                raise Exception('Following wrong node')
            f.signature = signature
            self.db.follow.set_follower(f)
            proto = Profile(self.db).get(False)
            m = Metadata()
            m.name = proto.name
            m.handle = proto.handle
            m.avatar_hash = proto.avatar_hash
            m.short_description = proto.short_description
            m.nsfw = proto.nsfw
            for listener in self.listeners:
                try:
                    verifyObject(NotificationListener, listener)
                    listener.notify(sender.id, f.metadata.handle, "follow", "", "", f.metadata.avatar_hash)
                except DoesNotImplement:
                    pass
            return ["True", m.SerializeToString(), self.signing_key.sign(m.SerializeToString())[:64]]
        except Exception:
            self.log.warning("failed to validate follower")
            return ["False"]

    def rpc_unfollow(self, sender, signature):
        self.log.info("received unfollow request from %s" % sender)
        self.router.addContact(sender)
        try:
            verify_key = nacl.signing.VerifyKey(sender.pubkey)
            verify_key.verify("unfollow:" + self.node.id, signature)
            f = self.db.follow
            f.delete_follower(sender.id)
            return ["True"]
        except Exception:
            self.log.warning("failed to validate signature on unfollow request")
            return ["False"]

    def rpc_get_followers(self, sender):
        self.log.info("serving followers list to %s" % sender)
        self.router.addContact(sender)
        ser = self.db.follow.get_followers()
        if ser is None:
            return None
        else:
            return [ser, self.signing_key.sign(ser)[:64]]

    def rpc_get_following(self, sender):
        self.log.info("serving following list to %s" % sender)
        self.router.addContact(sender)
        ser = self.db.follow.get_following()
        if ser is None:
            return None
        else:
            return [ser, self.signing_key.sign(ser)[:64]]

    def rpc_broadcast(self, sender, message, signature):
        if len(message) <= 140 and self.db.follow.is_following(sender.id):
            try:
                verify_key = nacl.signing.VerifyKey(sender.pubkey)
                verify_key.verify(message, signature)
            except Exception:
                self.log.warning("received invalid broadcast from %s" % sender)
                return ["False"]
            self.log.info("received a broadcast from %s" % sender)
            self.router.addContact(sender)
            for listener in self.listeners:
                try:
                    verifyObject(BroadcastListener, listener)
                    listener.notify(sender.id, message)
                except DoesNotImplement:
                    pass
            return ["True"]
        else:
            return ["False"]

    def rpc_message(self, sender, pubkey, encrypted):
        try:
            box = Box(self.signing_key.to_curve25519_private_key(), PublicKey(pubkey))
            plaintext = box.decrypt(encrypted)
            p = PlaintextMessage()
            p.ParseFromString(plaintext)
            signature = p.signature
            p.ClearField("signature")
            verify_key = nacl.signing.VerifyKey(p.pubkey)
            verify_key.verify(p.SerializeToString(), signature)
            h = nacl.hash.sha512(p.pubkey)
            pow_hash = h[40:]
            if int(pow_hash[:6], 16) >= 50 or p.sender_guid.encode("hex") != h[:40] or p.sender_guid != sender.id:
                raise Exception('Invalid guid')
            self.log.info("received a message from %s" % sender)
            self.router.addContact(sender)
            for listener in self.listeners:
                try:
                    verifyObject(MessageListener, listener)
                    listener.notify(p, signature)
                except DoesNotImplement:
                    pass
            return ["True"]
        except Exception:
            self.log.warning("received invalid message from %s" % sender)
            return ["False"]

    def rpc_order(self, sender, pubkey, encrypted):
        try:
            box = Box(self.signing_key.to_curve25519_private_key(), PublicKey(pubkey))
            order = box.decrypt(encrypted)
            c = Contract(self.db, contract=json.loads(order, object_pairs_hook=OrderedDict),
                         testnet=self.multiplexer.testnet)
            if c.verify(sender.pubkey):
                self.router.addContact(sender)
                self.log.info("received an order from %s, waiting for payment..." % sender)
                payment_address = c.contract["buyer_order"]["order"]["payment"]["address"]
                chaincode = c.contract["buyer_order"]["order"]["payment"]["chaincode"]
                masterkey_b = c.contract["buyer_order"]["order"]["id"]["pubkeys"]["bitcoin"]
                buyer_key = derive_childkey(masterkey_b, chaincode)
                amount = c.contract["buyer_order"]["order"]["payment"]["amount"]
                listing_hash = c.contract["vendor_offer"]["listing"]["contract_id"]
                signature = self.signing_key.sign(
                    str(payment_address) + str(amount) + str(listing_hash) + str(buyer_key))[:64]
                c.await_funding(self.get_notification_listener(), self.multiplexer.blockchain, signature, False)
                return [signature]
            else:
                self.log.warning("received invalid order from %s" % sender)
                return ["False"]
        except Exception:
            self.log.error("unable to decrypt order from %s" % sender)
            return ["False"]

    def rpc_order_confirmation(self, sender, pubkey, encrypted):
        try:
            box = Box(self.signing_key.to_curve25519_private_key(), PublicKey(pubkey))
            order = box.decrypt(encrypted)
            c = Contract(self.db, contract=json.loads(order, object_pairs_hook=OrderedDict),
                         testnet=self.multiplexer.testnet)
            contract_id = c.accept_order_confirmation(self.get_notification_listener())
            if contract_id:
                self.router.addContact(sender)
                self.log.info("received confirmation for order %s" % contract_id)
                return ["True"]
            else:
                self.log.warning("received invalid order confirmation from %s" % sender)
                return ["False"]
        except Exception:
            self.log.error("unable to decrypt order confirmation from %s" % sender)
            return ["False"]

    def rpc_complete_order(self, sender, pubkey, encrypted):
        try:
            box = Box(self.signing_key.to_curve25519_private_key(), PublicKey(pubkey))
            order = box.decrypt(encrypted)
            c = Contract(self.db, contract=json.loads(order, object_pairs_hook=OrderedDict),
                         testnet=self.multiplexer.testnet)

            contract_id = c.accept_receipt(self.get_notification_listener(), self.multiplexer.blockchain)
            self.router.addContact(sender)
            self.log.info("received receipt for order %s" % contract_id)
            return ["True"]
        except Exception:
            import traceback
            traceback.print_exc()
            self.log.error("unable to parse receipt from %s" % sender)
            return ["False"]

    def rpc_dispute_open(self, sender, pubkey, encrypted):
        try:
            box = Box(self.signing_key.to_curve25519_private_key(), PublicKey(pubkey))
            order = box.decrypt(encrypted)
            contract = json.loads(order, object_pairs_hook=OrderedDict)
            process_dispute(contract, self.db, self.get_message_listener(),
                            self.get_notification_listener(), self.multiplexer.testnet)
            self.router.addContact(sender)
            self.log.info("Contract dispute opened by %s" % sender)
            return ["True"]
        except Exception:
            self.log.error("unable to parse disputed contract from %s" % sender)
            return ["False"]

    def rpc_dispute_close(self, sender, pubkey, encrypted):
        try:
            box = Box(self.signing_key.to_curve25519_private_key(), PublicKey(pubkey))
            res = box.decrypt(encrypted)
            resolution_json = json.loads(res, object_pairs_hook=OrderedDict)
            close_dispute(resolution_json, self.db, self.get_message_listener(),
                          self.get_notification_listener(), self.multiplexer.testnet)
            self.router.addContact(sender)
            self.log.info("Contract dispute closed by %s" % sender)
            return ["True"]
        except Exception:
            self.log.error("unable to parse disputed close message from %s" % sender)
            return ["False"]

    def rpc_get_ratings(self, sender, listing_hash=None):
        a = "ALL" if listing_hash is None else listing_hash.encode("hex")
        self.log.info("serving ratings for contract %s to %s" % (a, sender))
        self.router.addContact(sender)
        try:
            ratings = []
            if listing_hash:
                for rating in self.db.ratings.get_listing_ratings(listing_hash.encode("hex")):
                    ratings.append(json.loads(rating[0], object_pairs_hook=OrderedDict))
            else:
                for rating in self.db.ratings.get_all_ratings():
                    ratings.append(json.loads(rating[0], object_pairs_hook=OrderedDict))
            ret = json.dumps(ratings).encode("zlib")
            return [str(ret), self.signing_key.sign(ret)[:64]]
        except Exception:
            self.log.warning("could not load ratings for contract %s" % a)
            return None

    def rpc_refund(self, sender, pubkey, encrypted):
        try:
            box = Box(self.signing_key.to_curve25519_private_key(), PublicKey(pubkey))
            refund = box.decrypt(encrypted)
            refund_json = json.loads(refund, object_pairs_hook=OrderedDict)
            order_id = refund_json["order_id"]

            file_path = DATA_FOLDER + "purchases/in progress/" + order_id + ".json"
            with open(file_path, 'r') as filename:
                order = json.load(filename, object_pairs_hook=OrderedDict)
            order["refund"] = refund_json["refund"]

            if "txid" not in refund_json:
                outpoints = pickle.loads(self.db.sales.get_outpoint(order_id))
                refund_address = order["buyer_order"]["order"]["refund_address"]
                redeem_script = order["buyer_order"]["order"]["payment"]["redeem_script"]
                value = int(float(refund_json["refund"]["value"]) * 100000000)
                tx = BitcoinTransaction.make_unsigned(outpoints, refund_address,
                                                      testnet=self.multiplexer.testnet,
                                                      out_value=value)
                chaincode = order["buyer_order"]["order"]["payment"]["chaincode"]
                masterkey_b = bitcointools.bip32_extract_key(KeyChain(self.db).bitcoin_master_privkey)
                buyer_priv = derive_childkey(masterkey_b, chaincode, bitcointools.MAINNET_PRIVATE)
                buyer_sigs = tx.create_signature(buyer_priv, redeem_script)
                vendor_sigs = refund_json["refund"]["signature(s)"]

                signatures = []
                for i in range(len(outpoints)):
                    for vendor_sig in vendor_sigs:
                        if vendor_sig["index"] == i:
                            v_signature = vendor_sig["signature"]
                    for buyer_sig in buyer_sigs:
                        if buyer_sig["index"] == i:
                            b_signature = buyer_sig["signature"]
                    signature_obj = {"index": i, "signatures": [b_signature, v_signature]}
                    signatures.append(signature_obj)

                tx.multisign(signatures, redeem_script)
                tx.broadcast(self.multiplexer.blockchain)
                self.log.info("Broadcasting refund tx %s to network" % tx.get_hash())

            self.db.sales.update_status(order_id, 7)
            file_path = DATA_FOLDER + "purchases/trade receipts/" + order_id + ".json"
            with open(file_path, 'w') as outfile:
                outfile.write(json.dumps(order, indent=4))
            file_path = DATA_FOLDER + "purchases/in progress/" + order_id + ".json"
            if os.path.exists(file_path):
                os.remove(file_path)

            title = order["vendor_offer"]["listing"]["item"]["title"]
            if "image_hashes" in order["vendor_offer"]["listing"]["item"]:
                image_hash = unhexlify(order["vendor_offer"]["listing"]["item"]["image_hashes"][0])
            else:
                image_hash = ""
            buyer_guid = self.contract["buyer_order"]["order"]["id"]["guid"]
            if "blockchain_id" in self.contract["buyer_order"]["order"]["id"]:
                handle = self.contract["buyer_order"]["order"]["id"]["blockchain_id"]
            else:
                handle = ""
            self.get_notification_listener().notify(buyer_guid, handle, "refund", order_id, title, image_hash)

            self.router.addContact(sender)
            self.log.info("order %s refunded by vendor" % refund_json["refund"]["order_id"])
            return ["True"]
        except Exception:
            self.log.error("unable to parse refund message from %s" % sender)
            return ["False"]

    def callGetContract(self, nodeToAsk, contract_hash):
        d = self.get_contract(nodeToAsk, contract_hash)
        return d.addCallback(self.handleCallResponse, nodeToAsk)

    def callGetImage(self, nodeToAsk, image_hash):
        d = self.get_image(nodeToAsk, image_hash)
        return d.addCallback(self.handleCallResponse, nodeToAsk)

    def callGetProfile(self, nodeToAsk):
        d = self.get_profile(nodeToAsk)
        return d.addCallback(self.handleCallResponse, nodeToAsk)

    def callGetUserMetadata(self, nodeToAsk):
        d = self.get_user_metadata(nodeToAsk)
        return d.addCallback(self.handleCallResponse, nodeToAsk)

    def callGetListings(self, nodeToAsk):
        d = self.get_listings(nodeToAsk)
        return d.addCallback(self.handleCallResponse, nodeToAsk)

    def callGetContractMetadata(self, nodeToAsk, contract_hash):
        d = self.get_contract_metadata(nodeToAsk, contract_hash)
        return d.addCallback(self.handleCallResponse, nodeToAsk)

    def callFollow(self, nodeToAsk, proto, signature):
        d = self.follow(nodeToAsk, proto, signature)
        return d.addCallback(self.handleCallResponse, nodeToAsk)

    def callUnfollow(self, nodeToAsk, signature):
        d = self.unfollow(nodeToAsk, signature)
        return d.addCallback(self.handleCallResponse, nodeToAsk)

    def callGetFollowers(self, nodeToAsk):
        d = self.get_followers(nodeToAsk)
        return d.addCallback(self.handleCallResponse, nodeToAsk)

    def callGetFollowing(self, nodeToAsk):
        d = self.get_following(nodeToAsk)
        return d.addCallback(self.handleCallResponse, nodeToAsk)

    def callBroadcast(self, nodeToAsk, message, signature):
        d = self.broadcast(nodeToAsk, message, signature)
        return d.addCallback(self.handleCallResponse, nodeToAsk)

    def callMessage(self, nodeToAsk, ehemeral_pubkey, ciphertext):
        d = self.message(nodeToAsk, ehemeral_pubkey, ciphertext)
        return d.addCallback(self.handleCallResponse, nodeToAsk)

    def callOrder(self, nodeToAsk, ephem_pubkey, encrypted_contract):
        d = self.order(nodeToAsk, ephem_pubkey, encrypted_contract)
        return d.addCallback(self.handleCallResponse, nodeToAsk)

    def callOrderConfirmation(self, nodeToAsk, ephem_pubkey, encrypted_contract):
        d = self.order_confirmation(nodeToAsk, ephem_pubkey, encrypted_contract)
        return d.addCallback(self.handleCallResponse, nodeToAsk)

    def callCompleteOrder(self, nodeToAsk, ephem_pubkey, encrypted_contract):
        d = self.complete_order(nodeToAsk, ephem_pubkey, encrypted_contract)
        return d.addCallback(self.handleCallResponse, nodeToAsk)

    def callDisputeOpen(self, nodeToAsk, ephem_pubkey, encrypted_contract):
        d = self.dispute_open(nodeToAsk, ephem_pubkey, encrypted_contract)
        return d.addCallback(self.handleCallResponse, nodeToAsk)

    def callDisputeClose(self, nodeToAsk, ephem_pubkey, encrypted_contract):
        d = self.dispute_close(nodeToAsk, ephem_pubkey, encrypted_contract)
        return d.addCallback(self.handleCallResponse, nodeToAsk)

    def callGetRatings(self, nodeToAsk, listing_hash=None):
        if listing_hash is None:
            d = self.get_ratings(nodeToAsk)
        else:
            d = self.get_ratings(nodeToAsk, listing_hash)
        return d.addCallback(self.handleCallResponse, nodeToAsk)

    def callRefund(self, nodeToAsk, order_id, refund):
        d = self.refund(nodeToAsk, order_id, refund)
        return d.addCallback(self.handleCallResponse, nodeToAsk)

    def handleCallResponse(self, result, node):
        """
        If we get a response, add the node to the routing table.  If
        we get no response, make sure it's removed from the routing table.
        """
        if result[0]:
            self.router.addContact(node)
        else:
            self.log.debug("no response from %s, removing from router" % node)
            self.router.removeContact(node)
        return result

    def get_notification_listener(self):
        for listener in self.listeners:
            try:
                verifyObject(NotificationListener, listener)
                return listener
            except DoesNotImplement:
                pass

    def get_message_listener(self):
        for listener in self.listeners:
            try:
                verifyObject(MessageListener, listener)
                return listener
            except DoesNotImplement:
                pass

    def __iter__(self):
        return iter(self.handled_commands)
