# -*- coding: utf-8 -*-
# Copyright 2017-2018 The pyXem developers
#
# This file is part of pyXem.
#
# pyXem is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# pyXem is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with pyXem.  If not, see <http://www.gnu.org/licenses/>.

"""Electron diffraction pattern simulation.

"""

import numpy as np
from math import sin, cos, asin, pi

from pyxem.signals.diffraction_simulation import DiffractionSimulation
from pyxem.signals.diffraction_simulation import ProfileSimulation

from pyxem.utils.atomic_scattering_params import ATOMIC_SCATTERING_PARAMS
from pyxem.utils.sim_utils import get_electron_wavelength,\
    get_kinematical_intensities, get_unique_families


class DiffractionGenerator(object):
    """Computes electron diffraction patterns for a crystal structure.

    1. Calculate reciprocal lattice of structure. Find all reciprocal points
       within the limiting sphere given by :math:`\\frac{2}{\\lambda}`.

    2. For each reciprocal point :math:`\\mathbf{g_{hkl}}` corresponding to
       lattice plane :math:`(hkl)`, compute the Bragg condition
       :math:`\\sin(\\theta) = \\frac{\\lambda}{2d_{hkl}}`

    3. The intensity of each reflection is then given in the kinematic
       approximation as the modulus square of the structure factor.
       :math:`I_{hkl} = F_{hkl}F_{hkl}^*`

    Parameters
    ----------
    accelerating_voltage : float
        The accelerating voltage of the microscope in kV.
    max_excitation_error : float
        The maximum extent of the relrods in reciprocal angstroms. Typically
        equal to 1/{specimen thickness}.
    debye_waller_factors : dict of str : float
        Maps element names to their temperature-dependent Debye-Waller factors.

    """
    # TODO: Refactor the excitation error to a structure property.

    def __init__(self,
                 accelerating_voltage,
                 max_excitation_error,
                 debye_waller_factors=None):
        self.wavelength = get_electron_wavelength(accelerating_voltage)
        self.max_excitation_error = max_excitation_error
        self.debye_waller_factors = debye_waller_factors or {}

    def calculate_ed_data(self,
                          structure,
                          reciprocal_radius,
                          with_direct_beam=True):
        """Calculates the Electron Diffraction data for a structure.

        Parameters
        ----------
        structure : Structure
            The structure for which to derive the diffraction pattern. Note that
            the structure must be rotated to the appropriate orientation and
            that testing is conducted on unit cells (rather than supercells).
        reciprocal_radius : float
            The maximum radius of the sphere of reciprocal space to sample, in
            reciprocal angstroms.

        Returns
        -------
        pyxem.DiffractionSimulation
            The data associated with this structure and diffraction setup.

        """
        # Specify variables used in calculation
        wavelength = self.wavelength
        max_excitation_error = self.max_excitation_error
        debye_waller_factors = self.debye_waller_factors
        latt = structure.lattice

        # Obtain crystallographic reciprocal lattice points within `max_r` and
        # g-vector magnitudes for intensity calculations.
        recip_latt = latt.reciprocal_lattice_crystallographic
        recip_pts, g_hkls = \
            recip_latt.get_points_in_sphere([[0, 0, 0]], [0, 0, 0],
                                            reciprocal_radius,
                                            zip_results=False)[:2]
        cartesian_coordinates = recip_latt.get_cartesian_coords(recip_pts)

        # Identify points intersecting the Ewald sphere within maximum
        # excitation error and store the magnitude of their excitation error.
        radius = 1 / wavelength
        r = np.sqrt(np.sum(np.square(cartesian_coordinates[:, :2]), axis=1))
        theta = np.arcsin(r / radius)
        z_sphere = radius * (1 - np.cos(theta))
        proximity = np.absolute(z_sphere - cartesian_coordinates[:, 2])
        intersection = proximity < max_excitation_error
        # Mask parameters corresponding to excited reflections.
        intersection_coordinates = cartesian_coordinates[intersection]
        intersection_indices = recip_pts[intersection]
        proximity = proximity[intersection]
        g_hkls = g_hkls[intersection]

        # Calculate diffracted intensities based on a kinematical model.
        intensities = get_kinematical_intensities(structure,
                                                  intersection_indices,
                                                  g_hkls,
                                                  proximity,
                                                  max_excitation_error,
                                                  debye_waller_factors)

        # Threshold peaks included in simulation based on minimum intensity.
        peak_mask = intensities > 1e-20
        intensities = intensities[peak_mask]
        intersection_coordinates = intersection_coordinates[peak_mask]
        intersection_indices = intersection_indices[peak_mask]

        return DiffractionSimulation(coordinates=intersection_coordinates,
                                     indices=intersection_indices,
                                     intensities=intensities,
                                     with_direct_beam=with_direct_beam)

    def calculate_profile_data(self, structure,
                               reciprocal_radius=1.0,
                               magnitude_tolerance=1e-5,
                               minimum_intensity=1e-3):
        """
        Calculates a one dimensional diffraction profile for a structure.

        Parameters
        ----------
        structure : Structure
            The structure for which to calculate the diffraction profile.
        reciprocal_radius : float
            The maximum radius of the sphere of reciprocal space to sample, in
            reciprocal angstroms.
        magnitude_tolerance : float
            The minimum difference between diffraction magnitudes in reciprocal
            angstroms for two peaks to be consdiered different.
        minimum_intensity : float
            The minimum intensity required for a diffraction peak to be
            considered real. Deals with numerical precision issues.

        Returns
        -------
        pyxem.ProfileSimulation
            The diffraction profile corresponding to this structure and
            experimental conditions.
        """
        max_r = reciprocal_radius
        wavelength = self.wavelength
        latt = structure.lattice
        is_hex = latt.is_hexagonal()

        # Obtain crystallographic reciprocal lattice points within range
        recip_latt = latt.reciprocal_lattice_crystallographic
        recip_pts = recip_latt.get_points_in_sphere(
            [[0, 0, 0]], [0, 0, 0], max_r)

        # Create a flattened array of zs, coeffs, fcoords and occus. This is
        # used to perform vectorized computation of atomic scattering factors
        # later. Note that these are not necessarily the same size as the
        # structure as each partially occupied specie occupies its own
        # position in the flattened array.
        zs = []
        coeffs = []
        fcoords = []
        occus = []
        dwfactors = []

        for site in structure:
            for sp, occu in site.species_and_occu.items():
                zs.append(sp.Z)
                try:
                    c = ATOMIC_SCATTERING_PARAMS[sp.symbol]
                except KeyError:
                    raise ValueError("Unable to calculate XRD pattern as "
                                     "there is no scattering coefficients for"
                                     " %s." % sp.symbol)
                coeffs.append(c)
                dwfactors.append(self.debye_waller_factors.get(sp.symbol, 0))
                fcoords.append(site.frac_coords)
                occus.append(occu)

        zs = np.array(zs)
        coeffs = np.array(coeffs)
        fcoords = np.array(fcoords)
        occus = np.array(occus)
        dwfactors = np.array(dwfactors)
        peaks = {}
        gs = []

        for hkl, g_hkl, ind in sorted(
                recip_pts, key=lambda i: (i[1], -i[0][0], -i[0][1], -i[0][2])):
            # Force miller indices to be integers.
            hkl = [int(round(i)) for i in hkl]
            if g_hkl != 0:

                d_hkl = 1 / g_hkl

                # Bragg condition
                #theta = asin(wavelength * g_hkl / 2)

                # s = sin(theta) / wavelength = 1 / 2d = |ghkl| / 2 (d =
                # 1/|ghkl|)
                s = g_hkl / 2

                # Store s^2 since we are using it a few times.
                s2 = s ** 2

                # Vectorized computation of g.r for all fractional coords and
                # hkl.
                g_dot_r = np.dot(fcoords, np.transpose([hkl])).T[0]

                # Highly vectorized computation of atomic scattering factors.
                fs = np.sum(coeffs[:, :, 0] * np.exp(-coeffs[:, :, 1] * s2), axis=1)

                dw_correction = np.exp(-dwfactors * s2)

                # Structure factor = sum of atomic scattering factors (with
                # position factor exp(2j * pi * g.r and occupancies).
                # Vectorized computation.
                f_hkl = np.sum(fs * occus * np.exp(2j * pi * g_dot_r)
                               * dw_correction)

                # Intensity for hkl is modulus square of structure factor.
                i_hkl = (f_hkl * f_hkl.conjugate()).real

                #two_theta = degrees(2 * theta)

                if is_hex:
                    # Use Miller-Bravais indices for hexagonal lattices.
                    hkl = (hkl[0], hkl[1], - hkl[0] - hkl[1], hkl[2])
                # Deal with floating point precision issues.
                ind = np.where(np.abs(np.subtract(gs, g_hkl)) <
                               magnitude_tolerance)
                if len(ind[0]) > 0:
                    peaks[gs[ind[0][0]]][0] += i_hkl
                    peaks[gs[ind[0][0]]][1].append(tuple(hkl))
                else:
                    peaks[g_hkl] = [i_hkl, [tuple(hkl)], d_hkl]
                    gs.append(g_hkl)

        # Scale intensities so that the max intensity is 100.
        max_intensity = max([v[0] for v in peaks.values()])
        x = []
        y = []
        hkls = []
        d_hkls = []
        for k in sorted(peaks.keys()):
            v = peaks[k]
            fam = get_unique_families(v[1])
            if v[0] / max_intensity * 100 > minimum_intensity:
                x.append(k)
                y.append(v[0])
                hkls.append(fam)
                d_hkls.append(v[2])

        y = y / max(y) * 100

        return ProfileSimulation(x, y, hkls)