from dataclasses import dataclass
from typing import List
from copy import deepcopy

import keystone
import z3

REGISTERS = {
    'rax', 'rbx', 'rcx', 'rdx', 'rdi', 'rsi', 'rbp', 'rsp',
    'r8', 'r9', 'r10', 'r11', 'r12', 'r13', 'r14', 'r15',
}
charset = b"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
charsetLength = len(charset)


@dataclass
class MulCacheStruct:
    word: int = 0
    byte: int = 0


@dataclass
class MulGadgetStruct:
    mul: MulCacheStruct = MulCacheStruct()
    offset: int = 0


@dataclass
class EncodeInfoStruct:
    idx: int = 0
    reg: str = 'rax'
    useLowByte: bool = False


@dataclass
class EncodeInfoPlusStruct:
    info: EncodeInfoStruct = EncodeInfoStruct()
    gadget: MulGadgetStruct = MulGadgetStruct()
    needPushByte: bool = False
    needChangeRdi: bool = False
    needChangeRdx: bool = False
    needRecoverRdx: bool = False


def isalnum(ch: int) -> bool:
    if 0x30 <= ch <= 0x39 \
            or 0x41 <= ch <= 0x5a \
            or 0x61 <= ch <= 0x7a:
        return True
    return False


class AE64:
    def __init__(self):
        # ---------- variables ----------
        # snippets asm
        self._initDecoderAsm: str
        self._clearRdiAsm: str
        self._nopAsm: str
        self._nop2Asm: str
        self._initDecoderSmallAsm: str
        self._lvl2DecoderTemplateAsm: str
        self._lvl2DecoderPatch: str

        # snippets bytes
        self._initDecoder: bytes
        self._clearRdi: bytes
        self._nop: bytes
        self._nop2: bytes
        self._initDecoderSmall: bytes
        self._lvl2DecoderTemplate: bytes

        # keystone engine
        self._ks: keystone.keystone.Ks

        # variables used while encoding
        self._encodeInfo: List[EncodeInfoStruct] = []
        self._encodeInfoPlus: List[EncodeInfoPlusStruct] = []

        # ---------- init functions ----------
        self._init_keystone()
        self._init_snippets()

    def _init_keystone(self):
        """
        initialize keystone engine
        """
        self._ks = keystone.Ks(keystone.KS_ARCH_X86, keystone.KS_MODE_64)

    def _init_snippets(self):
        """
        initialize some useful asm snippets
        """
        self._initDecoderAsm = '''
        /* set encoder */
        /* 0x6d57 x 0x33 == 0xc855 (200,85) rdx */
        /* 0x424a x 0x38 == 0x8030 (128,53) r8 */
        /* 0x436b x 0x4b == 0xc059 (192,89) r9 */
        /* 0x6933 x 0x43 == 0x8859 (136,89) r10 */

        push  0x33
        push  rsp
        pop   rcx
        imul  di, word ptr [rcx], 0x6d57
        push  rdi
        pop   rdx /* 0xc855 */

        push  0x38
        push  rsp
        pop   rcx
        imul  di, word ptr [rcx], 0x424a
        push  rdi
        pop   r8 /* 0x8030 */

        push  0x4b
        push  rsp
        pop   rcx
        imul  di, word ptr [rcx], 0x436b
        push  rdi
        pop   r9 /* 0xc059 */

        push  0x43
        push  rsp
        pop   rcx
        imul  di, word ptr [rcx], 0x6933
        push  rdi
        pop   r10 /* 0x8859 */
        '''

        self._clearRdiAsm = '''
        push  rdi
        push  rsp
        pop   rcx
        xor   rdi, [rcx]
        pop   rcx
        '''

        self._nopAsm = '''
        push rcx
        '''

        self._nop2Asm = '''
        push rcx
        pop rcx
        '''

        self._initDecoderSmallAsm = '''
        /* set encoder */
        /* 0x5970 x 0x6f == 0xc790 (199,144) rdx */
        
        push  0x6f
        push  rsp
        pop   rcx
        imul  di, word ptr [rcx], 0x5970
        push  rdi
        pop   rdx /* 0xc790 */
        
        push 0x41
        pop r8 /* 0x0041 */
        '''

        self._lvl2DecoderTemplateAsm = '''
        /* need encode: 0f 8d af c6 e9 ff */

        /* clean rsi */
        push rsi
        push rsp
        pop rcx
        xor rsi,[rcx]
        pop rcx

        /* get encode start */
        push 0x41
        push rsp
        pop rcx
        imul si, word ptr [rcx], 0x4141
        lea r14, [rax+rsi] /* RECOVER 1 byte (0x11: 0x8d) */

        /* get encode offset */
        push 0x42
        push rsp
        pop rcx
        imul si, word ptr [rcx], 0x4242
        push rsi
        pop rcx
        lea rsi, [r14+rsi*2] /* RECOVER 1 byte (0x20: 0x8d) */

        /* push rsi to stack */
        push rsi
        push rsp
        pop rax

        decoder_start:
        /* r14 -> ptr - 0x31 */
        movsxd rsi, dword ptr [r14+0x32]
        imul si, word ptr [rcx+r14+0x31] /* RECOVER 2 bytes (0x2c: 0x0f, 0x2d: 0xaf) */
        xor byte ptr [r14+0x31], sil

        inc r14 /* RECOVER 2 bytes (0x36: 0xff, 0x37: 0xc6) */
        cmp [rax],r14

        jne decoder_start /* RECOVER 1 byte (0x3c: 0xe9) */
        '''

        self._lvl2DecoderPatch = '''
        push {}
        push rsp
        pop rcx
        imul si, word ptr [rcx], {}
        '''

        # use keystone to assemble snippets
        self._initDecoder, _ = self._ks.asm(self._initDecoderAsm, as_bytes=True)
        self._clearRdi, _ = self._ks.asm(self._clearRdiAsm, as_bytes=True)
        self._nop, _ = self._ks.asm(self._nopAsm, as_bytes=True)
        self._nop2, _ = self._ks.asm(self._nop2Asm, as_bytes=True)
        self._initDecoderSmall, _ = self._ks.asm(self._initDecoderSmallAsm, as_bytes=True)
        self._lvl2DecoderTemplate, _ = self._ks.asm(self._lvl2DecoderTemplateAsm, as_bytes=True)

    def _gen_prologue(self, register: str) -> bytes:
        ans = b""
        if register != 'rax':
            ans += self.gen_machine_code("push {};pop rax;".format(register))
        ans += self._clearRdi + self._initDecoder
        return ans

    def _gen_prologue_small(self, register: str) -> bytes:
        ans = b""
        if register != 'rax':
            ans += self.gen_machine_code("push {};pop rax;".format(register))
        ans += self._clearRdi + self._initDecoderSmall
        return ans

    def _gen_encoded_shellcode(self, sc: bytes) -> bytes:
        regs = ['rdx', 'r8', 'r9', 'r10']
        lBytes = [0x55, 0x30, 0x59, 0x59]
        hBytes = [0xc8, 0x80, 0xc0, 0x88]
        res = bytearray(sc)
        length = len(sc)

        self._encodeInfo.clear()
        for i in range(length):
            if isalnum(sc[i]):
                continue
            tmpInfo = EncodeInfoStruct()
            tmpInfo.idx = i
            if sc[i] < 0x80:
                # use dl to do xor
                tmpInfo.useLowByte = True
                for j in range(4):
                    if isalnum(lBytes[j] ^ sc[i]):
                        tmpInfo.reg = regs[j]
                        res[i] ^= lBytes[j]
                        break
            else:
                # use dh to do xor
                tmpInfo.useLowByte = False
                for j in range(4):
                    if isalnum(hBytes[j] ^ sc[i]):
                        tmpInfo.reg = regs[j]
                        res[i] ^= hBytes[j]
                        break
            self._encodeInfo.append(tmpInfo)
        return bytes(res)

    def _gen_encoded_small_lvl2_decoder(self, sc: bytes) -> bytes:
        reg_rdx = [0x90, 0xc7]  # low, high
        reg_r8 = [0x41, 0]  # low, high
        res = bytearray(sc)
        length = len(sc)

        self._encodeInfo.clear()
        for i in range(length):
            if isalnum(sc[i]):
                continue
            tmpInfo = EncodeInfoStruct()
            tmpInfo.idx = i
            if sc[i] < 0x80:
                # only need encode 0xf, we assume in r8
                tmpInfo.useLowByte = True
                tmpInfo.reg = 'r8'
                res[i] ^= reg_r8[0]
            else:
                tmpInfo.reg = 'rdx'
                for j in range(2):
                    if isalnum(reg_rdx[j] ^ sc[i]):
                        tmpInfo.useLowByte = True if j == 0 else False
                        res[i] ^= reg_rdx[j]
                        break
            self._encodeInfo.append(tmpInfo)
        return bytes(res)

    def _gen_decoder(self, offset: int) -> bytes:
        decoderAsm = ""
        self._optimize_encoder_info(offset)
        for infoPlus in self._encodeInfoPlus:
            if infoPlus.needChangeRdi:
                if infoPlus.needPushByte:
                    decoderAsm += "push {};push rsp;pop rcx;".format(infoPlus.gadget.mul.byte)
                decoderAsm += "imul di, [rcx], {};\n".format(infoPlus.gadget.mul.word)
            if infoPlus.info.reg != 'rdx' and infoPlus.needChangeRdx:
                decoderAsm += "push rdx;push {};pop rdx;\n".format(infoPlus.info.reg)
            if infoPlus.info.useLowByte:
                decoderAsm += "xor [rax + rdi + {}], dl;\n".format(infoPlus.gadget.offset)
            else:
                decoderAsm += "xor [rax + rdi + {}], dh;\n".format(infoPlus.gadget.offset)
            if infoPlus.info.reg != 'rdx' and infoPlus.needRecoverRdx:
                decoderAsm += "pop rdx;\n"
        decoder = self.gen_machine_code(decoderAsm)
        return decoder

    def _optimize_encoder_info(self, offset: int):
        def gen_single_info():
            nonlocal cacheRdi, cacheStackByte, needPushByte
            mulGadget: MulGadgetStruct = MulGadgetStruct()
            target = self._encodeInfo[i].idx + offset
            for offIdx in range(charsetLength):
                # optimize 1. use old stack byte
                if cacheStackByte:
                    for highByte in range(charsetLength):
                        for lowByte in range(charsetLength):
                            mulWord = (charset[highByte] << 8) + charset[lowByte]
                            ans = (mulWord * cacheStackByte) & 0xffff
                            if ans + charset[offIdx] == target:
                                cacheRdi = ans
                                needPushByte = False
                                mulGadget.mul.byte = cacheStackByte
                                mulGadget.mul.word = mulWord
                                mulGadget.offset = target - ans
                                tmpInfo.needPushByte = False
                                tmpInfo.needChangeRdi = False
                                tmpInfo.gadget = mulGadget
                                return
                # can't use old stack byte
                for highByte in range(charsetLength):
                    for lowByte in range(charsetLength):
                        mulWord = (charset[highByte] << 8) + charset[lowByte]
                        for b in range(charsetLength):
                            mulByte = charset[b]
                            ans = (mulWord * mulByte) & 0xffff
                            if ans + charset[offIdx] == target:
                                cacheRdi = ans
                                cacheStackByte = mulByte
                                needPushByte = True
                                mulGadget.mul.byte = cacheStackByte
                                mulGadget.mul.word = mulWord
                                mulGadget.offset = target - ans
                                tmpInfo.needPushByte = False
                                tmpInfo.needChangeRdi = False
                                tmpInfo.gadget = mulGadget
                                return
            raise Exception("can't find mul gadget, this should not happen")

        self._encodeInfoPlus.clear()
        count = len(self._encodeInfo)
        lastUpdate = 0
        book = [0 for _ in range(count)]

        cacheRdi = 0
        cacheStackByte = 0

        useRdx: List[EncodeInfoPlusStruct] = []
        useR8: List[EncodeInfoPlusStruct] = []
        useR9: List[EncodeInfoPlusStruct] = []
        useR10: List[EncodeInfoPlusStruct] = []

        tmpInfo: EncodeInfoPlusStruct

        noUpdate: bool
        needCalcNewRdi: bool
        needChangeRdi: bool
        needPushByte: bool

        while True:
            noUpdate = True
            needCalcNewRdi = True
            needChangeRdi = True
            needPushByte = True
            for i in range(lastUpdate, count):
                tmpInfo = EncodeInfoPlusStruct()
                if book[i]:
                    continue
                if needCalcNewRdi:
                    needCalcNewRdi = False
                    lastUpdate = i
                    gen_single_info()
                # optimize 2. try to use old rdi
                if isalnum(self._encodeInfo[i].idx + offset - cacheRdi):
                    noUpdate = False
                    book[i] = 1
                    # optimize 3. try to use old rdx
                    tmpInfo.info = self._encodeInfo[i]
                    tmpInfo.needChangeRdx = False
                    tmpInfo.needRecoverRdx = False
                    tmpInfo.gadget.offset = self._encodeInfo[i].idx + offset - cacheRdi
                    if self._encodeInfo[i].reg == 'rdx':
                        useRdx.append(deepcopy(tmpInfo))
                    elif self._encodeInfo[i].reg == 'r8':
                        useR8.append(deepcopy(tmpInfo))
                    elif self._encodeInfo[i].reg == 'r9':
                        useR9.append(deepcopy(tmpInfo))
                    elif self._encodeInfo[i].reg == 'r10':
                        useR10.append(deepcopy(tmpInfo))
            # end of "for i in range(lastUpdate, count)"
            if len(useRdx) > 0:
                useRdx[0].needChangeRdx = True
                useRdx[-1].needRecoverRdx = True
            if len(useR8) > 0:
                useR8[0].needChangeRdx = True
                useR8[-1].needRecoverRdx = True
            if len(useR9) > 0:
                useR9[0].needChangeRdx = True
                useR9[-1].needRecoverRdx = True
            if len(useR10) > 0:
                useR10[0].needChangeRdx = True
                useR10[-1].needRecoverRdx = True
            useRdx.extend(useR8)
            useRdx.extend(useR9)
            useRdx.extend(useR10)
            if len(useRdx) > 0:
                useRdx[0].needChangeRdi = needChangeRdi
                useRdx[0].needPushByte = needPushByte
                self._encodeInfoPlus.extend(useRdx)
            useRdx.clear()
            useR8.clear()
            useR9.clear()
            useR10.clear()
            if noUpdate:
                break
        return

    def _gen_small_level1_decoder(self, offset: int) -> bytes:
        decoderAsm = ""
        self._optimize_encoder_info(offset)
        for infoPlus in self._encodeInfoPlus:
            if infoPlus.needChangeRdi:
                if infoPlus.needPushByte:
                    decoderAsm += "push {};push rsp;pop rcx;".format(infoPlus.gadget.mul.byte)
                decoderAsm += "imul di, [rcx], {};\n".format(infoPlus.gadget.mul.word)
            if infoPlus.info.reg != 'rdx' and infoPlus.needChangeRdx:
                decoderAsm += "push rdx;push {};pop rdx;\n".format(infoPlus.info.reg)
            if infoPlus.info.useLowByte:
                decoderAsm += "xor [rax + rdi + {}], dl;\n".format(infoPlus.gadget.offset)
            else:
                decoderAsm += "xor [rax + rdi + {}], dh;\n".format(infoPlus.gadget.offset)
            if infoPlus.info.reg != 'rdx' and infoPlus.needRecoverRdx:
                decoderAsm += "pop rdx;\n"
        decoder = self.gen_machine_code(decoderAsm)
        return decoder

    def _gen_small_encoded_shellcode(self, sc: bytes) -> (bytes, int):
        if len(sc) % 2:
            # padding to even length
            sc += b'\x00'
        scLength = len(sc)
        offset = len(sc) // 2
        if offset < 4:
            offset = 4
        targetLength = scLength + offset
        ans = [z3.BitVec('e_{}'.format(i), 8) for i in range(targetLength)]
        s = z3.Solver()
        for i in range(targetLength):
            s.add(z3.Or(
                z3.And(0x30 <= ans[i], ans[i] <= 0x39),
                z3.And(0x41 <= ans[i], ans[i] <= 0x5a),
                z3.And(0x61 <= ans[i], ans[i] <= 0x7a),
            ))
        for i in range(scLength):
            s.add((ans[i + 1] * ans[i + offset]) ^ ans[i] == sc[i])
        if s.check() == z3.unsat:
            raise Exception("encode unsat")
        m = s.model()
        return bytes([m[ans[i]].as_long() for i in range(targetLength)]), offset

    def _patch_level2_decoder(self, shellcode: bytes, start: int, offset: int) -> bytes:
        def get_mul_pair(num: int) -> (int, int):
            def z3_isalnum(ch):
                return z3.Or(
                    z3.And(0x30 <= ch, ch <= 0x39),
                    z3.And(0x41 <= ch, ch <= 0x5a),
                    z3.And(0x61 <= ch, ch <= 0x7a),
                )

            v = [z3.BitVec(f'v_{i}', 16) for i in range(2)]
            s = z3.Solver()
            s.add(v[0] & 0xff00 == 0)
            s.add(v[0] * v[1] == num)
            s.add(v[0] * v[1] == num)
            s.add(z3_isalnum(v[0] & 0xff))
            s.add(z3_isalnum(v[1] & 0xff))
            s.add(z3_isalnum((v[1] & 0xff00) >> 8))
            if s.check() == z3.unsat:
                raise Exception("encode unsat")
            m = s.model()
            return m[v[0]].as_long(), m[v[1]].as_long()

        b1, w1 = get_mul_pair(start - 0x31)
        b2, w2 = get_mul_pair(offset)
        shellcode = shellcode.replace(
            self.gen_machine_code(self._lvl2DecoderPatch.format(0x41, 0x4141)),
            self.gen_machine_code(self._lvl2DecoderPatch.format(b1, w1)),
        )
        shellcode = shellcode.replace(
            self.gen_machine_code(self._lvl2DecoderPatch.format(0x42, 0x4242)),
            self.gen_machine_code(self._lvl2DecoderPatch.format(b2, w2)),
        )
        return shellcode

    def gen_machine_code(self, asm_code: str) -> bytes:
        """
        use keystone to assemble

        @param asm_code: asm code
        @return: bytecode
        """
        if not self._ks:
            raise Exception("keystone not initialized")
        ans, _ = self._ks.asm(asm_code, as_bytes=True)
        return ans

    def encode_fast(self, shellcode: bytes, register: str = 'rax', offset: int = 0) -> bytes:
        """
        use fast generate strategy to encode given shellcode into alphanumeric shellcode (amd64 only)
        @param shellcode: bytes format shellcode
        @param register: the register contains shellcode pointer (can with offset) (default=rax)
        @param offset: the offset (default=0)
        @return: encoded shellcode
        """
        # 1. get prologue
        if register.lower() not in REGISTERS:
            raise Exception("register name '{}' is not valid".format(register))
        prologue = self._gen_prologue(register.lower())
        prologueLength = len(prologue)
        print("[+] prologue generated")

        # 2. get encoded shellcode
        encodedShellcode = self._gen_encoded_shellcode(shellcode)
        for ch in encodedShellcode:
            if not isalnum(ch):
                raise Exception("find non-alphanumeric byte {} in encodedShellcode".format(hex(ch)))
        print("[+] encoded shellcode generated")

        # 3. build decoder
        totalSpace = prologueLength if prologueLength > 0x20 else 0x20
        while True:
            print("[*] build decoder, try free space: {} ...".format(totalSpace))
            decoder = self._gen_decoder(offset + totalSpace)
            decoderLength = len(decoder)
            trueLength = prologueLength + decoderLength
            if totalSpace >= trueLength and totalSpace - trueLength <= 100:
                # suitable length, not too long and not too short
                nopLength = totalSpace - trueLength
                break
            totalSpace = trueLength

        new_shellcode = prologue + decoder
        new_shellcode += self._nop2 * (nopLength // 2)
        new_shellcode += self._nop * (nopLength % 2)
        new_shellcode += encodedShellcode

        # do some check
        for ch in new_shellcode:
            if not isalnum(ch):
                raise Exception("find non-alphanumeric byte {} in final shellcode".format(hex(ch)))

        print("[+] Alphanumeric shellcode generate successfully!")
        print("[+] Total length: {}".format(len(new_shellcode)))
        return new_shellcode

    def encode_small(self, shellcode: bytes, register: str = 'rax', offset: int = 0) -> bytes:
        """
        use small generate strategy to encode given shellcode into alphanumeric shellcode (amd64 only)
        @param shellcode: bytes format shellcode
        @param register: the register contains shellcode pointer (can with offset) (default=rax)
        @param offset: the offset (default=0)
        @return: encoded shellcode
        """
        # 1. get prologue
        if register.lower() not in REGISTERS:
            raise Exception("register name '{}' is not valid".format(register))
        prologue = self._gen_prologue_small(register.lower())
        prologueLength = len(prologue)
        print("[+] prologue generated")

        # 2. build encoded level2 decoder
        encodedLvl2DecoderTemplate = self._gen_encoded_small_lvl2_decoder(self._lvl2DecoderTemplate)
        for ch in encodedLvl2DecoderTemplate:
            if not isalnum(ch):
                raise Exception("find non-alphanumeric byte {} in encodedShellcode".format(hex(ch)))

        # 3. build level1 decoder
        totalSpace = prologueLength if prologueLength > 0x20 else 0x20
        while True:
            print("[*] build decoder, try free space: {} ...".format(totalSpace))
            decoder = self._gen_small_level1_decoder(offset + totalSpace)
            decoderLength = len(decoder)
            trueLength = prologueLength + decoderLength
            if totalSpace >= trueLength and totalSpace - trueLength <= 100:
                # suitable length, not too long and not too short
                nopLength = totalSpace - trueLength
                break
            totalSpace = trueLength

        # 4. build encoded shellcode
        print("[*] generate encoded shellcode ...".format(totalSpace))
        encodedShellcode, encoder2Offset = self._gen_small_encoded_shellcode(shellcode)

        # 5. patch lvl2 decoder template
        encoder2Start = offset + len(prologue) + len(decoder) + nopLength + len(encodedLvl2DecoderTemplate)
        encodedLvl2Decoder = self._patch_level2_decoder(encodedLvl2DecoderTemplate, encoder2Start, encoder2Offset)

        new_shellcode = prologue + decoder
        new_shellcode += self._nop2 * (nopLength // 2)
        new_shellcode += self._nop * (nopLength % 2)
        new_shellcode += encodedLvl2Decoder
        new_shellcode += encodedShellcode

        print("[+] Alphanumeric shellcode generate successfully!")
        print("[+] Total length: {}".format(len(new_shellcode)))
        return new_shellcode

    def encode(self, shellcode: bytes, register: str = 'rax', offset: int = 0, strategy: str = 'fast') -> bytes:
        """
        encode given shellcode into alphanumeric shellcode (amd64 only)
        @param shellcode: bytes format shellcode
        @param register: the register contains shellcode pointer (can with offset) (default=rax)
        @param offset: the offset (default=0)
        @param strategy: encode strategy, can be "fast" or "small" (default=fast)
        @return: encoded shellcode
        """
        if strategy.lower() not in ['fast', 'small']:
            raise Exception("strategy neither 'fast' nor 'small'")

        if strategy.lower() == 'fast':
            return self.encode_fast(shellcode, register, offset)
        else:
            return self.encode_small(shellcode, register, offset)