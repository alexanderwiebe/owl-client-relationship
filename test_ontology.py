#!/usr/bin/env python3
"""
Test script to verify emmontopy installation and load the relationship ontology
"""

import sys
from pathlib import Path

try:
    import emmopy
    from emmopy import World
    print("✓ emmopy imported successfully")
    print(f"emmopy version: {emmopy.__version__}")
except ImportError as e:
    print(f"✗ Failed to import emmopy: {e}")
    sys.exit(1)


def test_ontology():
    """Load and analyze the relationship ontology"""

    # Path to the ontology file
    ontology_path = Path("relationship.ttl")

    if not ontology_path.exists():
        print(f"✗ Ontology file not found: {ontology_path}")
        return

    print(f"\n📁 Loading ontology from: {ontology_path}")

    try:
        # Create a world and load the ontology
        world = World()
        onto = world.get_ontology(f"file://{ontology_path.absolute()}")
        onto.load()

        print("✓ Ontology loaded successfully")

        # Get ontology metadata
        print(f"\n📋 Ontology Information:")
        print(f"   Base IRI: {onto.base_iri}")
        print(f"   Name: {onto.name}")

        # Get classes
        print(f"\n🏷️  Classes:")
        classes = list(onto.classes())
        if classes:
            for cls in sorted(classes, key=lambda x: x.name):
                # Get label if available
                label = getattr(cls, 'label', [])
                label_str = f" ({label[0]})" if label else ""

                # Get superclasses
                superclasses = [sc.name for sc in cls.is_a if hasattr(
                    sc, 'name') and sc != cls]
                super_str = f" ⊑ {', '.join(superclasses)}" if superclasses else ""

                print(f"   • {cls.name}{label_str}{super_str}")
        else:
            print("   No classes found")

        # Get object properties
        print(f"\n🔗 Object Properties:")
        object_properties = list(onto.object_properties())
        if object_properties:
            for prop in sorted(object_properties, key=lambda x: x.name):
                # Get label if available
                label = getattr(prop, 'label', [])
                label_str = f" ({label[0]})" if label else ""

                # Get domain and range
                domain = getattr(prop, 'domain', [])
                range_prop = getattr(prop, 'range', [])

                domain_str = f" Domain: {[d.name for d in domain if hasattr(d, 'name')]}" if domain else ""
                range_str = f" Range: {[r.name for r in range_prop if hasattr(r, 'name')]}" if range_prop else ""

                # Get superproperties
                superprops = [sp.name for sp in prop.is_a if hasattr(
                    sp, 'name') and sp != prop]
                super_str = f" ⊑ {', '.join(superprops)}" if superprops else ""

                print(f"   • {prop.name}{label_str}{super_str}")
                if domain_str or range_str:
                    print(f"     {domain_str}{range_str}")
        else:
            print("   No object properties found")

        # Get data properties
        print(f"\n📊 Data Properties:")
        data_properties = list(onto.data_properties())
        if data_properties:
            for prop in sorted(data_properties, key=lambda x: x.name):
                label = getattr(prop, 'label', [])
                label_str = f" ({label[0]})" if label else ""
                print(f"   • {prop.name}{label_str}")
        else:
            print("   No data properties found")

        # Get individuals
        print(f"\n👤 Individuals:")
        individuals = list(onto.individuals())
        if individuals:
            for ind in sorted(individuals, key=lambda x: x.name):
                # Get types
                types = [t.name for t in ind.is_a if hasattr(t, 'name')]
                type_str = f" : {', '.join(types)}" if types else ""

                label = getattr(ind, 'label', [])
                label_str = f" ({label[0]})" if label else ""

                print(f"   • {ind.name}{label_str}{type_str}")
        else:
            print("   No individuals found")

        print(f"\n✅ Ontology analysis complete!")

    except Exception as e:
        print(f"✗ Error loading ontology: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    test_ontology()
