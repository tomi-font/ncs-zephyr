# Copyright (c) 2026 Nordic Semiconductor ASA
#
# SPDX-License-Identifier: Apache-2.0

'''Runner for flashing an application booted by IronSide TE.'''

import json
import subprocess
import sys
from pathlib import Path

from runners.nrf_common import ErrNotAvailableBecauseProtection, ErrVerify
from runners.nrfutil import NrfUtilBinaryRunner

# Devicetree labels of the partitions the application owns. Everything else in
# an IronSide TE layout - the roots of trust, their secure storage, the DFU
# agent, the write-once UICR configuration, the KMU - belongs to IronSide TE.
APP_PARTITIONS = ('slot0_partition', 'dfubank_partition', 'storage_partition')

REINSTALL = 'ironsdk/scripts/ironside_install.py <bundle> --force'

# PSA lifecycle states in which the debugger may program the application over
# SWD. In every other state the application partition is not writable from the
# debugger, and the application is installed through IronSide DFU instead.
SWD_LIFECYCLES = {
    0x1000: 'Open',
    0x2000: 'PRoT Provisioning',
}

RECOVER_REFUSED = f'''--recover is not available on a target running IronSide TE.

It is an unauthenticated mass erase and unlock, with no version of it scoped to
the application: an unprotected device would lose IronSide TE along with
everything else, and a protected device refuses it outright.

Reinstalling IronSide TE is what brings a device back:

  {REINSTALL}'''

ERASE_KEPT = f'''
IronSide TE is left intact, unlike normal --erase: the roots of trust, their
secure storage, the DFU agent, the write-once UICR configuration and the KMU,
the persistent keys, the OTA key-encryption keys and the rollback counters
all survive. An image below the resident security counter is therefore still
refused after this erase. To clear that too, reinstall IronSide TE:

  {REINSTALL}'''

PROTECTED = f'''Flashing over SWD failed because the target is protected.

A Secured device is flashed through IronSide DFU instead, which this runner
picks by itself with --flash-path=auto, the default:

  west flash --flash-path=dfu

Reinstalling IronSide TE removes the protection along with everything else on
the device, and needs the keyset the device was provisioned with:

  {REINSTALL} --keyset <keyset>'''

DFU_ERASE_IGNORED = '''IronSide DFU writes the application in place and
erases nothing else, so --erase and --erase-mode have no effect here.'''

UICR_IN_IMAGE = '''The image has data in the UICR region and the verify failed,
which usually means the UICR was not erased first. Here it cannot be: the UICR
configuration belongs to IronSide TE.'''


class IronSideTEBinaryRunner(NrfUtilBinaryRunner):
    '''Runner front-end for nrfutil on a target booted by IronSide TE.

    In the Open and PRoT Provisioning lifecycle states, flashing is unchanged
    from the nrfutil runner. What differs is the reach of the two options that
    erase beyond the image: a chip erase and a recover would both take IronSide
    TE and the write-once UICR configuration with them, leaving a device that
    only a reinstall brings back.

    In every other state, such as Secured, the debugger cannot write the
    application partition, so the image is installed through IronSide DFU with
    the IronSDK app_update.py instead.
    '''

    def __init__(self, cfg, *args, flash_path='auto', keyset=None,
                 ironsdk_dir=None, **kwargs):
        super().__init__(cfg, *args, **kwargs)

        self.flash_path = flash_path
        self.keyset = keyset
        self.ironsdk_dir = Path(ironsdk_dir) if ironsdk_dir else None

    @classmethod
    def name(cls):
        return 'ironside_te'

    @classmethod
    def do_create(cls, cfg, args):
        return cls(cfg, args.nrf_family, args.softreset, args.pinreset,
                   args.dev_id, erase=args.erase, erase_mode=args.erase_mode,
                   ext_erase_mode=args.ext_erase_mode, reset=args.reset,
                   tool_opt=args.tool_opt, force=args.force,
                   recover=args.recover,
                   ext_mem_config_file=args.ext_mem_config_file,
                   dry_run=args.dry_run, flash_path=args.flash_path,
                   keyset=args.keyset, ironsdk_dir=args.ironsdk_dir)

    @classmethod
    def do_add_parser(cls, parser):
        super().do_add_parser(parser)

        parser.add_argument('--flash-path', choices=('auto', 'swd', 'dfu'),
                            default='auto',
                            help='''how to install the application: swd
                            programs it with nrfutil, dfu uploads it through
                            IronSide DFU, and auto (the default) reads the
                            lifecycle state over ADAC and uses swd in Open
                            and PRoT Provisioning, dfu otherwise''')
        parser.add_argument('--keyset',
                            help='''IronSDK key source the image was signed
                            with, as keyring:NAME or dir:PATH; the dfu path
                            encrypts the image with its OTA key''')
        parser.add_argument('--ironsdk-dir',
                            help='IronSDK root, holding scripts/')

    def do_run(self, command, **kwargs):
        # Refused here rather than in recover_target(), so that it fails before
        # the base class has gone looking for a board.
        if self.recover:
            raise RuntimeError(RECOVER_REFUSED)

        if command == 'flash':
            # Twister passes --dev-id, and hardware maps that predate that
            # pass it again through runner_params: it is still one board.
            if isinstance(self.dev_id, list) and len(set(self.dev_id)) == 1:
                self.dev_id = self.dev_id[0]
            self.ensure_snr()

            if self._choose_flash_path() == 'dfu':
                self._flash_dfu()
                return

        super().do_run(command, **kwargs)

    def _choose_flash_path(self):
        if self.dry_run:
            path = 'swd' if self.flash_path == 'auto' else self.flash_path
            self.logger.info(f'Dry run: flashing through {path}, without '
                             'reading the lifecycle state.')
            return path

        # Every ADAC command is normally a session of its own, left with a reset
        # into a normal boot, and each of those boots the old application, whose
        # output a test harness already on the console takes for the new one's.
        # One session reads the lifecycle state and leaves into the DFU agent
        # instead, which waits there for either flash path.
        self._adac('enter')
        try:
            lifecycle = None
            if self.flash_path == 'auto':
                lifecycle = self._read_lifecycle()
            self._adac('--command-only', 'boot-mode', 'dfu')
        finally:
            self._adac('exit')

        if self.flash_path != 'auto':
            self.logger.info(f'Flashing through {self.flash_path}, as requested.')
            return self.flash_path

        state = SWD_LIFECYCLES.get(lifecycle)
        if state:
            self.logger.info(f'Lifecycle state {state} (0x{lifecycle:04x}): '
                             'flashing over SWD.')
            return 'swd'

        self.logger.info(f'Lifecycle state 0x{lifecycle:04x} does not allow '
                         'programming over SWD: flashing through IronSide DFU.')
        return 'dfu'

    def _read_lifecycle(self):
        '''Return the PSA lifecycle state that ADAC discovery reports. The
        CTRL-AP that carries ADAC is reachable in every lifecycle state.'''
        out = self._adac('--command-only', '--json', 'discovery')

        try:
            return int(json.loads(out)['psa_lifecycle'])
        except (ValueError, KeyError, TypeError) as e:
            raise RuntimeError(
                f'Could not read the lifecycle state from ADAC discovery: {out!r}. '
                'Pass --flash-path=swd or --flash-path=dfu to skip it.') from e

    def _flash_dfu(self):
        if not self.keyset:
            raise RuntimeError(
                'Flashing through IronSide DFU encrypts the image with the OTA '
                'key of its keyset, and no --keyset was given. The build passes '
                'CONFIG_IRONSIDE_TE_KEYSET here by itself.')

        if self.erase or self.erase_mode:
            self.logger.warning(DFU_ERASE_IGNORED)

        self.ensure_output('hex')
        self.logger.info(f'Flashing file: {self.hex_}')

        # The DFU agent is already up, so app_update.py only uploads the image
        # over its SMP port and resets into the new application.
        self.check_call([sys.executable, str(self._ironsdk_script('app_update.py')),
                         self.hex_, '--keyset', self.keyset,
                         '--serial-number', self._dev_id(),
                         '--skip-adac-bootmode-set'])

    def _adac(self, *args):
        return self.check_output([sys.executable, str(self._ironsdk_script('adac.py')),
                                  '--serial-number', self._dev_id(), *args])

    def _ironsdk_script(self, name):
        if self.ironsdk_dir is None:
            raise RuntimeError('The IronSDK location is unknown; pass --ironsdk-dir.')

        script = self.ironsdk_dir / 'scripts' / name
        if not script.is_file():
            raise RuntimeError(f'{script} not found; is --ironsdk-dir the IronSDK root?')

        return script

    def _dev_id(self):
        # The IronSDK tools take a single board; ensure_snr() resolved it.
        if isinstance(self.dev_id, list):
            raise RuntimeError('Flashing several IronSide TE devices at once is '
                               'not supported; pass a single --dev-id.')

        return self.dev_id

    def flush(self, force=False):
        # The base class explains both of these failures by pointing at
        # --recover, the one operation this runner does not have.
        try:
            self.flush_ops(force=force)
        except subprocess.CalledProcessError as cpe:
            if cpe.returncode == ErrNotAvailableBecauseProtection:
                self.logger.error(PROTECTED)
            elif cpe.returncode == ErrVerify and self.hex_get_uicrs():
                self.logger.warning(UICR_IN_IMAGE)
            raise

    def op_program(self, hex_file, erase, ext_mem_erase, defer=False, core=None):
        # Both --erase and --erase-mode=all arrive here as a chip erase, which
        # would take the roots of trust and the UICR page with it.
        if erase == 'ERASE_ALL':
            self._erase_app_partitions()
            erase = 'ERASE_NONE'

        super().op_program(hex_file, erase, ext_mem_erase, defer=defer, core=core)

    def _append_batch(self, op, json_file):
        operation = op['operation']

        # The base class spells an erase as a bare --<kind> flag, which leaves
        # nowhere to put a page range.
        if operation['type'] == 'erase' and operation.get('kind') == 'pages':
            self._exec(['erase', '--pages', operation['pages'],
                        '--x-family', f'{self.family}',
                        '--x-append-batch', f'{json_file}'], force=True)
            return

        super()._append_batch(op, json_file)

    def _erase_app_partitions(self):
        partitions = self._app_partitions()
        ranges = self._erase_ranges(partitions)

        report = ["Erasing the application's partitions only:"]
        report += [f'  {label:<19}0x{addr:08x}  {size // 1024:>4} kB'
                   for label, addr, size in partitions]
        report.append(ERASE_KEPT)
        self.logger.info('\n'.join(report))

        for start, end in ranges:
            # nrfutil is given the last address to erase, which it documents
            # both as excluded and as naming the page to stop at. A byte inside
            # the range satisfies both.
            self.exec_op('erase', defer=True, kind='pages',
                         pages=f'0x{start:08x}-0x{end - 1:08x}')

    def _app_partitions(self):
        '''Return the application's partitions as (label, address, size),
        lowest address first.'''
        edt = self.build_conf.edt

        partitions = []
        for label in APP_PARTITIONS:
            node = edt.label2node.get(label)
            if node is not None:
                partitions.append((label, node.regs[0].addr, node.regs[0].size))

        if not partitions:
            raise RuntimeError(
                f'This build has none of {", ".join(APP_PARTITIONS)} in its '
                'devicetree, so there is no way to tell which partitions the '
                'application owns. Is this an IronSide TE board target?')

        partitions.sort(key=lambda partition: partition[1])

        return partitions

    def _erase_ranges(self, partitions):
        '''Return the (start, end) spans to erase, end excluded. Partitions
        that touch are coalesced, so a contiguous layout takes a single erase
        and a gap between two partitions is never erased.'''
        ranges = []
        for _, addr, size in partitions:
            if ranges and ranges[-1][1] == addr:
                ranges[-1][1] = addr + size
            else:
                ranges.append([addr, addr + size])

        ranges = [tuple(span) for span in ranges]
        self._check_page_aligned(ranges)

        return ranges

    def _check_page_aligned(self, ranges):
        # An erase reaches whole pages, so both edges of every span have to sit
        # on a page boundary to stay within the partitions it covers. Boundaries
        # between coalesced partitions do not matter; the span's own edges do,
        # IronSide TE being on the other side.
        page_size = self._page_size()

        for start, end in ranges:
            if start % page_size or end % page_size:
                raise RuntimeError(
                    "An erase of the application's partitions would run "
                    f'0x{start:08x}-0x{end:08x}, which the {page_size} byte '
                    'erase page does not divide, so it would reach into '
                    'IronSide TE. Compare the devicetree layout against '
                    'ironsdk/scripts/read_partitions.py.')

    def _page_size(self):
        flash = self.build_conf.edt.chosen_node('zephyr,flash')
        page = flash.props.get('erase-block-size') if flash else None
        page_size = page.val if page else 0
        if not page_size:
            raise RuntimeError(
                'The devicetree does not say how large an erase page is, so '
                "the erase cannot be held inside the application's partitions.")

        return page_size
