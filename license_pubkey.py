"""Ed25519 PUBLIC key used to verify MintScope Pro licenses offline (base64 of the 32 raw bytes).

Empty until the owner runs `python admin/license_admin.py keygen` on their own machine, which writes the
private key to a file OUTSIDE this project and puts the matching public key here. A public key is not secret.
"""
PUBLIC_KEY_B64 = ""
