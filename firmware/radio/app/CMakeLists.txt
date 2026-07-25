cmake_minimum_required(VERSION 3.20.0)

find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(uwb_phase1_bringup)

target_sources(app PRIVATE src/main.c)

if(CONFIG_USB_DEVICE_STACK_NEXT)
  target_sources(app PRIVATE src/usb_cir_stream.c)
  target_sources(app PRIVATE ${ZEPHYR_BASE}/samples/subsys/usb/common/sample_usbd_init.c)
  target_include_directories(app PRIVATE ${ZEPHYR_BASE}/samples/subsys/usb/common)
endif()

if(CONFIG_PHASE1_ROLE_SCHEDULED_TX OR CONFIG_PHASE1_ROLE_SENSING_RX)
  target_sources(app PRIVATE src/primitives.c)
endif()

if(CONFIG_PHASE1_ROLE_TWR_INITIATOR OR CONFIG_PHASE1_ROLE_TWR_RESPONDER)
  target_sources(app PRIVATE src/twr.c)
endif()
