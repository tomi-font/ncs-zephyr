# SPDX-License-Identifier: Apache-2.0

board_set_flasher_ifnset(ironside_te)

# A Secured device is flashed through IronSide DFU, which encrypts the image
# with the OTA key of the keyset that signed it.
board_runner_args(ironside_te "--ironsdk-dir=${ZEPHYR_IRONSIDE_INTEGRATION_KIT_MODULE_DIR}")
if(CONFIG_IRONSIDE_TE_KEYSET)
  board_runner_args(ironside_te "--keyset=${CONFIG_IRONSIDE_TE_KEYSET}")
endif()

board_finalize_runner_args(ironside_te)
