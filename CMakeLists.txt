cmake_minimum_required(VERSION 3.16)
project(DicomApiFetcher)

#-----------------------------------------------------------------------------
# Extension meta-information
set(EXTENSION_NAME DicomApiFetcher)
set(EXTENSION_HOMEPAGE "https://github.com/yourusername/DicomApiFetcher")
set(EXTENSION_CATEGORY "DICOM")
set(EXTENSION_CONTRIBUTORS "Your Name")
set(EXTENSION_DESCRIPTION "Fetch DICOM images from a remote HTTP API and load them into 3D Slicer.")
set(EXTENSION_ICONURL "")
set(EXTENSION_SCREENSHOTURLS "")
set(EXTENSION_DEPENDS "NA")

#-----------------------------------------------------------------------------
find_package(Slicer REQUIRED)
include(${Slicer_USE_FILE})

#-----------------------------------------------------------------------------
add_subdirectory(DicomApiFetcher)

#-----------------------------------------------------------------------------
include(${Slicer_EXTENSION_GENERATE_CONFIG})
include(${Slicer_EXTENSION_CPACK})
