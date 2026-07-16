var wms_layers = [];


        var lyr_GoogleHybrid_0 = new ol.layer.Tile({
            'title': 'Google Hybrid',
            'opacity': 1.000000,
            
            
            source: new ol.source.XYZ({
            attributions: '<a href="https://www.google.at/permissions/geoguidelines/attr-guide.html">Map data ©2015 Google</a>',
                url: 'https://mt1.google.com/vt/lyrs=y&x={x}&y={y}&z={z}'
            })
        });
var lyr_Inundable_1 = new ol.layer.Image({
        opacity: 1,
        
    title: 'Inundable<br />\
    <img src="styles/legend/Inundable_1_0.png" /> 0<br />\
    <img src="styles/legend/Inundable_1_1.png" /> 1<br />' ,
        
        
        source: new ol.source.ImageStatic({
            url: "./layers/Inundable_1.png",
            attributions: ' ',
            projection: 'EPSG:3857',
            alwaysInRange: true,
            imageExtent: [-9512475.516492, 1188276.811368, -9509250.194038, 1192929.373708]
        })
    });
var format_Area_interes_2 = new ol.format.GeoJSON();
var features_Area_interes_2 = format_Area_interes_2.readFeatures(json_Area_interes_2, 
            {dataProjection: 'EPSG:4326', featureProjection: 'EPSG:3857'});
var jsonSource_Area_interes_2 = new ol.source.Vector({
    attributions: ' ',
});
jsonSource_Area_interes_2.addFeatures(features_Area_interes_2);
var lyr_Area_interes_2 = new ol.layer.Vector({
                declutter: false,
                source:jsonSource_Area_interes_2, 
                style: style_Area_interes_2,
                popuplayertitle: 'Area_interes',
                interactive: true,
                title: '<img src="styles/legend/Area_interes_2.png" /> Area_interes'
            });

lyr_GoogleHybrid_0.setVisible(true);lyr_Inundable_1.setVisible(true);lyr_Area_interes_2.setVisible(true);
var layersList = [lyr_GoogleHybrid_0,lyr_Inundable_1,lyr_Area_interes_2];
lyr_Area_interes_2.set('fieldAliases', {'id': 'id', });
lyr_Area_interes_2.set('fieldImages', {'id': 'TextEdit', });
lyr_Area_interes_2.set('fieldLabels', {'id': 'no label', });
lyr_Area_interes_2.on('precompose', function(evt) {
    evt.context.globalCompositeOperation = 'normal';
});